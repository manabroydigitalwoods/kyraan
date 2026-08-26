"""The memory engine: classification, supersession, expiry, and
priority-ranked retrieval."""
import json
from datetime import datetime, timedelta, timezone

from kyraan.memory import engine, store


def test_migrate_backfills_from_tree_once():
    (store.MEMORY_ROOT / "people").mkdir(parents=True)
    (store.MEMORY_ROOT / "people" / "wife.md").write_text("- Wife's name is Mira\n")
    (store.MEMORY_ROOT / "work").mkdir()
    (store.MEMORY_ROOT / "work" / "job.md").write_text("- CTO at Acmeworks\n")

    assert engine.migrate_from_tree() == 2
    assert engine.migrate_from_tree() == 0  # second run: no-op
    entries = engine.active_entries()
    spheres = {e["content"]: e["sphere"] for e in entries}
    assert spheres["CTO at Acmeworks"] == "work"
    assert spheres["Wife's name is Mira"] == "personal"


def test_supersession_deactivates_the_old_fact():
    engine.add_fact("Son's name is Ishan", "people/son.md", "s1")
    new_id = engine.add_fact("Son's name is Aarav Roy", "people/son.md", "s2",
                             supersedes="Son's name is Ishan")
    active = engine.active_entries()
    assert [e["content"] for e in active] == ["Son's name is Aarav Roy"]
    # the old fact is history, not gone
    all_entries = engine._load()
    old = next(e for e in all_entries if e["content"] == "Son's name is Ishan")
    assert old["active"] is False and old["superseded_by"] == new_id


def test_safety_flagged_facts_always_ride_along():
    engine.add_fact("Allergic to penicillin", "people/owner.md", "s",
                    importance="critical", flags=["health"])
    for i in range(50):
        engine.add_fact(f"Prefers filler fact number {i}", "preferences/x.md", "s")

    context = engine.build_context("what's the weather like", budget_chars=800)
    assert "Allergic to penicillin" in context
    assert "[HEALTH]" in context


def test_relevance_ranks_matching_facts_into_a_tight_budget():
    engine.add_fact("Wife's name is Mira", "people/wife.md", "s", kind="relationship")
    for i in range(30):
        engine.add_fact(f"Random preference {i} about lorem ipsum", "preferences/x.md", "s")
    context = engine.build_context("tell me about my wife Mira", budget_chars=400)
    assert "Mira" in context


def test_past_era_facts_rank_up_when_reaching_for_the_past():
    engine.add_fact("Used to work at TCS", "work/history.md", "s", era="past", sphere="work")
    engine.add_fact("Currently CTO at Acmeworks", "work/job.md", "s", sphere="work")
    for i in range(20):
        engine.add_fact(f"Neutral fact {i} entirely unrelated", "preferences/x.md", "s")

    past_context = engine.build_context("where did I use to work before?", budget_chars=300)
    assert "TCS" in past_context

    present_context = engine.build_context("summarize my work situation now", budget_chars=10_000)
    assert "TCS" in present_context  # old memories are kept, just quieter


def test_short_term_facts_expire():
    fact_id = engine.add_fact("Going to Nagpur this week", "routines/trip.md", "s",
                              term="short", kind="situational")
    entries = engine._load()
    for entry in entries:
        if entry["id"] == fact_id:
            entry["created"] = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    engine._save(entries)
    assert all(e["id"] != fact_id for e in engine.active_entries())


def test_promote_registers_with_the_engine(tmp_path):
    proposal = store.propose_fact(
        "people/son.md", "- Son's name is Aarav Roy", source="rename son",
        meta={"term": "long", "importance": "high", "era": "current",
              "sphere": "personal", "flags": [], "supersedes": "Son's name is Ishan"})
    engine.add_fact("Son's name is Ishan", "people/son.md", "old")
    store.promote(proposal)
    active = [e["content"] for e in engine.active_entries()]
    assert "- Son's name is Aarav Roy" in " ".join(active) or "Son's name is Aarav Roy" in active
    assert "Son's name is Ishan" not in active


def test_sensitive_memories_stay_private_without_direct_relevance():
    """Discretion means absence: a sensitive fact never rides into an
    unrelated answer, but a direct question about it brings it — tagged
    so the model answers with care."""
    engine.add_fact("Marriage counseling sessions on Thursdays",
                    "routines/counseling.md", "s", flags=["sensitive", "emotional"])
    engine.add_fact("Wife's name is Mira", "people/wife.md", "s")

    casual = engine.build_context("what's on my calendar today")
    assert "counseling" not in casual.lower()

    direct = engine.build_context("when are the marriage counseling sessions?")
    assert "counseling" in direct.lower()
    assert "[EMOTIONAL/SENSITIVE]" in direct or "[SENSITIVE/EMOTIONAL]" in direct \
        or "SENSITIVE" in direct


def test_add_fact_is_idempotent_for_promote_retries():
    """Review P2: a retried promote must not double-index the fact."""
    first = engine.add_fact("Wife's name is Mira", "people/wife.md", "s")
    second = engine.add_fact("Wife's name is Mira", "people/wife.md", "s")
    assert first == second
    assert sum(1 for e in engine.active_entries()
               if e["content"] == "Wife's name is Mira") == 1
