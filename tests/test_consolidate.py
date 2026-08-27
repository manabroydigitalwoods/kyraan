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
