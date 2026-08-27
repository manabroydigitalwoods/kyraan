"""Semantic consolidation: scan validation (hallucinated ids, overlaps,
self-reference dropped) and the apply semantics (supersession, not
deletion — and never a forget-sweep)."""
import json

import pytest

from kyraan.memory import consolidate, engine


class _FakeResponse:
    def __init__(self, payload):
        self.text = json.dumps(payload)


def _seed(*contents):
    return [engine.add_fact(c, "preferences/dup.md", "test") for c in contents]


def test_scan_validates_model_output(monkeypatch):
    a, b, c = _seed("Kiaan born around October 2025",
                    "Kiaan was born on 12-10-2025",
                    "Favourite snack is murukku")
    from kyraan.model_router import router
    monkeypatch.setattr(router, "call", lambda **kw: _FakeResponse({"groups": [
        {"keep": b, "duplicates": [a], "reason": "same birthday"},
        {"keep": "ffffffff", "duplicates": [c], "reason": "hallucinated keep"},
        {"keep": c, "duplicates": [c], "reason": "self reference"},
        {"keep": a, "duplicates": [c], "reason": "overlaps group 1"},
    ]}))
    proposals = consolidate.scan()
    assert len(proposals) == 1
    assert proposals[0]["keep"] == b
    assert proposals[0]["duplicates"] == [(a, "Kiaan born around October 2025")]


def test_apply_supersedes_not_deletes():
    a, b = _seed("vague statement of the thing", "precise statement of the thing")
    gone = consolidate.apply(b, [a])
    assert gone == ["vague statement of the thing"]
    entries = {e["id"]: e for e in engine._load()}
    assert entries[a]["active"] is False
    assert entries[a]["superseded_by"] == b  # history, not deletion
    assert entries[b]["active"] is True
    active = [e["content"] for e in engine.active_entries()]
    assert "vague statement of the thing" not in active
    assert "precise statement of the thing" in active


def test_apply_refuses_inactive_keeper():
    a, b = _seed("one", "two")
    engine.forget([b])
    with pytest.raises(ValueError, match="not an active fact"):
        consolidate.apply(b, [a])


def test_apply_is_not_a_forget(monkeypatch):
    # supersession must NOT sweep episodes — the topic stays live
    from kyraan.store import episodes, facts
    monkeypatch.setattr(facts, "MIRROR_ENABLED", True)
    monkeypatch.setattr(facts, "mirror_entries", lambda entries: True)
    swept = []
    monkeypatch.setattr(episodes, "suppress_for_fact",
                        lambda fid, content: swept.append(fid) or 0)
    a, b = _seed("older wording", "newer wording")
    consolidate.apply(b, [a])
    assert swept == []


def test_apply_skips_unknown_and_already_inactive():
    a, b = _seed("one thing", "same thing, better")
    engine.forget([a])
    assert consolidate.apply(b, [a, "nosuchid"]) == []


# --- the chat surface ("consolidate memory") ------------------------------

def _proposal(keep, keep_content, dups):
    return {"keep": keep, "keep_content": keep_content,
            "duplicates": dups, "reason": "same fact"}


async def test_chat_phrase_asks_with_every_group_named(monkeypatch):
    from kyraan.agents import orchestrator
    a, b = _seed("born around October", "born on 12-10-2025")
    monkeypatch.setattr(consolidate, "scan", lambda: [
        _proposal(b, "born on 12-10-2025", [(a, "born around October")])])
    reply = await orchestrator._dispatch(920_001, "consolidate memory")
    assert 'keep "born on 12-10-2025"' in reply
    assert 'supersede "born around October"' in reply
    assert 'reply "yes"' in reply


async def test_chat_yes_applies_the_stashed_proposals(monkeypatch):
    from kyraan.agents import orchestrator
    a, b = _seed("vague thing", "precise thing")
    monkeypatch.setattr(consolidate, "scan", lambda: [
        _proposal(b, "precise thing", [(a, "vague thing")])])
    await orchestrator._dispatch(920_002, "consolidate memory")
    reply = await orchestrator._dispatch(920_002, "yes")
    assert "1 duplicate fact(s) are now history" in reply
    entries = {e["id"]: e for e in engine._load()}
    assert entries[a]["active"] is False and entries[a]["superseded_by"] == b


async def test_chat_no_applies_nothing(monkeypatch):
    from kyraan.agents import orchestrator
    a, b = _seed("vague thing", "precise thing")
    monkeypatch.setattr(consolidate, "scan", lambda: [
        _proposal(b, "precise thing", [(a, "vague thing")])])
    await orchestrator._dispatch(920_003, "consolidate memory")
    reply = await orchestrator._dispatch(920_003, "no")
    assert "nothing was done" in reply.lower()
    assert {e["id"]: e for e in engine._load()}[a]["active"] is True


async def test_chat_clean_store_never_gates(monkeypatch):
    from kyraan.agents import orchestrator
    monkeypatch.setattr(consolidate, "scan", lambda: [])
    reply = await orchestrator._dispatch(920_004, "dedupe memory")
    assert "clean" in reply.lower() and 'reply "yes"' not in reply


async def test_chat_scan_failure_is_honest(monkeypatch):
    from kyraan.agents import orchestrator

    def boom():
        raise RuntimeError("cloud down")

    monkeypatch.setattr(consolidate, "scan", boom)
    reply = await orchestrator._dispatch(920_005, "consolidate memory")
    assert "isn't reachable" in reply
