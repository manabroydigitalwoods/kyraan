"""Tests for the fact-extraction half of the memory loop: stated facts get
queued for review, everything else — questions, malformed output, unsafe
paths — is dropped without error. Model calls are mocked; the store is
redirected to a tmp tree so no test touches real memory."""
from dataclasses import dataclass

import pytest

from kyraan.memory import extraction, store
from kyraan.model_router import router


@dataclass
class _FakeRouted:
    text: str


@pytest.fixture
def isolated_memory(monkeypatch, tmp_path):
    root = tmp_path / "memory"
    pending = root / "pending_review"
    pending.mkdir(parents=True)
    monkeypatch.setattr(store, "MEMORY_ROOT", root)
    monkeypatch.setattr(store, "PENDING_DIR", pending)
    yield pending


def _mock_model(monkeypatch, text: str) -> None:
    monkeypatch.setattr(router, "call", lambda **kwargs: _FakeRouted(text=text))


async def test_stated_fact_is_queued_for_review_not_written_live(monkeypatch, isolated_memory):
    _mock_model(monkeypatch, '{"facts": [{"path": "people/wife.md", "content": "- Wife\'s name is Mira"}]}')

    queued = await extraction.propose_from_message("my wife's name is Mira")

    assert queued == ["- Wife's name is Mira"]
    proposals = list(isolated_memory.glob("*.md"))
    assert len(proposals) == 1
    body = proposals[0].read_text()
    assert "target: people/wife.md" in body
    assert "my wife's name is Mira" in body  # verbatim source, for the reviewer
    assert store.list_fact_files() == []  # nothing live until promoted


async def test_fence_wrapped_json_still_parses(monkeypatch, isolated_memory):
    """llama3.1:8b sometimes wraps its JSON in a markdown fence — seen live
    in intent classification. Extraction must strip it, not drop the fact."""
    _mock_model(
        monkeypatch,
        '```json\n{"facts": [{"path": "preferences/tea.md", "content": "- Prefers tea over coffee"}]}\n```',
    )

    queued = await extraction.propose_from_message("I prefer tea over coffee")
    assert queued == ["- Prefers tea over coffee"]


async def test_no_facts_means_no_files_and_no_note(monkeypatch, isolated_memory):
    _mock_model(monkeypatch, '{"facts": []}')

    assert await extraction.propose_from_message("what time is it?") == []
    assert list(isolated_memory.glob("*")) == []


async def test_malformed_model_output_is_swallowed(monkeypatch, isolated_memory):
    _mock_model(monkeypatch, "sure! here are the facts you asked for:")

    assert await extraction.propose_from_message("my wife's name is Mira") == []
    assert list(isolated_memory.glob("*")) == []


async def test_unsafe_path_is_dropped_but_valid_facts_survive(monkeypatch, isolated_memory):
    """A model-generated path outside the memory layout (traversal, wrong
    category, wrong extension) must be rejected fact-by-fact, not crash the
    batch."""
    _mock_model(
        monkeypatch,
        '{"facts": ['
        '{"path": "../../.env", "content": "- evil"}, '
        '{"path": "people/wife.md", "content": "- Wife\'s name is Mira"}]}',
    )

    queued = await extraction.propose_from_message("my wife's name is Mira")

    assert queued == ["- Wife's name is Mira"]
    assert len(list(isolated_memory.glob("*.md"))) == 1


def test_propose_fact_rejects_paths_outside_the_memory_layout(isolated_memory):
    for bad in ("../evil.md", "/etc/passwd", "people/../work/x.md", "secrets/x.md", "people/x.txt"):
        with pytest.raises(ValueError):
            store.propose_fact(bad, "- x", source="x")


def test_load_all_facts_reads_live_tree_only(isolated_memory):
    live = store.MEMORY_ROOT / "people"
    live.mkdir()
    (live / "owner.md").write_text("- Name: Arun Verma\n")
    proposal = store.propose_fact("people/wife.md", "- unreviewed", source="test")

    facts = store.load_all_facts()
    assert "Name: Arun Verma" in facts
    assert "unreviewed" not in facts  # pending proposals are not live facts
    proposal.unlink()


async def test_questions_never_reach_the_extraction_model(monkeypatch, isolated_memory):
    """Seen live: "who is ruma?" produced a proposal despite the prompt's
    never-extract-from-questions rule. Enforced in code now — a trailing
    question mark skips extraction entirely (no model call at all)."""
    def explode(**kwargs):
        raise AssertionError("model should not be called for a question")

    monkeypatch.setattr(router, "call", explode)
    assert await extraction.propose_from_message("who is ruma?") == []
    assert list(isolated_memory.glob("*")) == []


async def test_long_pastes_never_reach_extraction(monkeypatch, isolated_memory):
    """Seen live: a pasted Wikipedia biography produced two junk proposals,
    one targeting a nonsense path. Articles are not personal statements."""
    def explode(**kwargs):
        raise AssertionError("model must not be called for a long paste")

    monkeypatch.setattr(router, "call", explode)
    assert await extraction.propose_from_message("Mamata Banerjee " * 200) == []


async def test_fabricated_facts_sharing_no_words_are_dropped(monkeypatch, isolated_memory):
    """Walkthrough v3 (degraded mode): 'make it 4 lines' produced 'Name is
    Anupam' + two more invented facts. Zero content-word overlap with the
    message = hallucination, dropped deterministically."""
    _mock_model(monkeypatch, '{"facts": ['
        '{"path": "people/anupam.md", "content": "- Name is Anupam"}, '
        '{"path": "preferences/tea.md", "content": "- Favourite tea is masala chai"}]}')

    queued = await extraction.propose_from_message("my favourite tea is masala chai")
    assert queued == ["- Favourite tea is masala chai"]  # the fabricated one is gone
    assert len(list(isolated_memory.glob("*.md"))) == 1


async def test_extraction_is_frontier_first_with_local_fallback(monkeypatch, isolated_memory):
    tiers = []

    def fake_call(prompt, system="", tier="cheap", **kwargs):
        tiers.append(tier)
        if tier == "frontier":
            raise __import__("kyraan.model_router.router", fromlist=["x"]).ModelProviderError("429")
        return _FakeRouted(text='{"facts": []}')

    monkeypatch.setattr(router, "call", fake_call)
    await extraction.propose_from_message("my favourite tea is masala chai")
    assert tiers == ["frontier", "cheap"]


async def test_restating_a_known_fact_is_not_reproposed(monkeypatch, isolated_memory):
    """Seen live: a duplicate wife-name proposal. Live + pending fact lines
    are the dedup baseline."""
    live = store.MEMORY_ROOT / "people"
    live.mkdir()
    (live / "ruma.md").write_text("- Wife's name is Mira\n")
    _mock_model(monkeypatch, '{"facts": [{"path": "people/wife.md", "content": "- Wife\'s name is Mira"}]}')

    queued = await extraction.propose_from_message("my wife's name is Mira")
    assert queued == []
    assert list(isolated_memory.glob("*.md")) == []


async def test_pending_proposals_also_count_as_known(monkeypatch, isolated_memory):
    _mock_model(monkeypatch, '{"facts": [{"path": "preferences/tea.md", "content": "- Favourite tea is masala chai"}]}')
    first = await extraction.propose_from_message("my favourite tea is masala chai")
    second = await extraction.propose_from_message("my favourite tea is masala chai")
    assert first and second == []
    assert len(list(isolated_memory.glob("*.md"))) == 1
