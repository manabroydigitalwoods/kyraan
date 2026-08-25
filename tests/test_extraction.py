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
