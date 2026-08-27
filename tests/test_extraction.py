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
    """Seen live: "who is mira?" produced a proposal despite the prompt's
    never-extract-from-questions rule. Enforced in code now — a trailing
    question mark skips extraction entirely (no model call at all)."""
    def explode(**kwargs):
        raise AssertionError("model should not be called for a question")

    monkeypatch.setattr(router, "call", explode)
    assert await extraction.propose_from_message("who is mira?") == []
    assert list(isolated_memory.glob("*")) == []


async def test_long_pastes_never_reach_extraction(monkeypatch, isolated_memory):
    """Seen live: a pasted Wikipedia biography produced two junk proposals,
    one targeting a nonsense path. Articles are not personal statements."""
    def explode(**kwargs):
        raise AssertionError("model must not be called for a long paste")

    monkeypatch.setattr(router, "call", explode)
    assert await extraction.propose_from_message("A Famous Politician " * 200) == []


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
    (live / "mira.md").write_text("- Wife's name is Mira\n")
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


async def test_context_resolves_referents_into_self_contained_facts(monkeypatch, isolated_memory):
    """'His name is deven rao' after a father question reached the queue
    unable to say who Deven was. With conversation context, the extractor
    produces a self-contained fact — and the fabrication guard accepts
    'Father' because the referent word comes from the context."""
    _mock_model(monkeypatch, '{"facts": [{"path": "people/father.md", "content": "- Father\'s name is Deven Rao"}]}')

    queued = await extraction.propose_from_message(
        "His name is deven rao",
        context="user: Do you know my father?\nassistant: I don't know it yet.",
    )
    assert queued == ["- Father's name is Deven Rao"]


async def test_fabrication_guard_holds_even_with_context(monkeypatch, isolated_memory):
    _mock_model(monkeypatch, '{"facts": [{"path": "people/anupam.md", "content": "- Name is Anupam"}]}')

    queued = await extraction.propose_from_message(
        "make it 4 lines",
        context="user: write 2 lines about tea\nassistant: Tea is lovely...",
    )
    assert queued == []  # Anupam appears in neither message nor context


async def test_capitalized_category_paths_are_normalized(monkeypatch, isolated_memory):
    """Seen live in the eval run: 'Preferences/murukku.md' (capital P from
    the model) was rejected by the lowercase-only validator, silently
    losing a clean stated fact."""
    _mock_model(monkeypatch, '{"facts": [{"path": "Preferences/murukku.md", "content": "- Favourite snack is murukku"}]}')
    queued = await extraction.propose_from_message("my favourite snack is murukku")
    assert queued == ["- Favourite snack is murukku"]


async def test_explicit_save_passes_insist_and_context_to_the_model(monkeypatch, tmp_path):
    """'save the aarav age' pointed at an earlier statement and extracted
    nothing, silently. An explicit save escalates: the prompt insists, and
    the referenced statement in context becomes legitimate material."""
    memory_root = tmp_path / "memory"
    (memory_root / "pending_review").mkdir(parents=True)
    monkeypatch.setattr(extraction.store, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(extraction.store, "PENDING_DIR", memory_root / "pending_review")
    captured = {}

    def fake_call(prompt, system="", **kwargs):
        captured["system"] = system
        class R: text = '{"facts": [{"path": "people/aarav.md", "content": "- Son Aarav was born around October 2025"}]}'
        return R()

    monkeypatch.setattr(extraction.router, "call", fake_call)
    queued = await extraction.propose_from_message(
        "save the aarav age",
        context="user: aarav is about 10months old\nassistant: Got it.",
        insist=True,
    )
    assert "EXPLICITLY asked to save" in captured["system"]
    assert queued == ["- Son Aarav was born around October 2025"]


async def test_paraphrased_category_normalizes_instead_of_dropping(
        monkeypatch, isolated_memory):
    """Found live 2026-08-27: a real fact about Kiaan's vaccination card
    was silently DROPPED because the model invented the "personal/"
    category — same normalize-the-paraphrase lesson as flag tagging.
    Traversal and truly unknown categories still reject."""
    _mock_model(
        monkeypatch,
        '{"facts": ['
        '{"path": "personal/kiaan_vaccination_monitor.md", '
        '"content": "- Has a vaccination monitor card for son Kiaan"}]}',
    )
    queued = await extraction.propose_from_message(
        "this is kiaan's vaccination monitor")
    assert queued == ["- Has a vaccination monitor card for son Kiaan"]
    saved = list(isolated_memory.glob("*.md"))
    assert len(saved) == 1


def test_normalize_path_maps_aliases_only():
    assert extraction._normalize_path("personal/kiaan_card.md") \
        == "people/kiaan_card.md"
    assert extraction._normalize_path("habits/tea.md") == "routines/tea.md"
    assert extraction._normalize_path("people/wife.md") == "people/wife.md"
    assert extraction._normalize_path("../../.env") == "../../.env"  # still dies in validation
