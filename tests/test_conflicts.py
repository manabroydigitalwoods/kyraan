"""Cross-person contradiction detection (the multi-user audit fix):
model proposes, the validator disposes, applying produces exactly the
P3.5d dispute state — resolvable by the existing review flow."""
import json

import pytest

from kyraan.control_plane import kernel
from kyraan.memory import conflicts, engine
from kyraan.memory import store as memory_store


class _FakeResponse:
    def __init__(self, payload):
        self.text = json.dumps(payload)


def _fact_as(person, content, target="people/kiaan.md"):
    token = kernel.set_viewer(person, "full" if person != "owner" else "owner")
    try:
        return engine.add_fact(content, target, "said")
    finally:
        kernel.reset_viewer_stage(token)


def _mock_scan(monkeypatch, conflict_pairs):
    from kyraan.model_router import router
    monkeypatch.setattr(router, "call", lambda **kw: _FakeResponse(
        {"conflicts": [{"a": a, "b": b, "reason": "same attribute"}
                       for a, b in conflict_pairs]}))


def test_cross_author_conflict_is_validated_and_ordered(monkeypatch):
    a = _fact_as("owner", "Kiaan's school starts at 9am")
    b = _fact_as("ruma", "Kiaan's school starts at 8am")
    _mock_scan(monkeypatch, [(b, a)])  # model order is IGNORED
    pairs = conflicts.scan()
    assert len(pairs) == 1
    assert pairs[0]["earlier"]["id"] == a   # created first, deterministically
    assert pairs[0]["later"]["id"] == b


def test_same_author_pairs_are_dropped(monkeypatch):
    a = _fact_as("owner", "Dinner is at 8pm")
    b = _fact_as("owner", "Dinner is at 9pm")
    _mock_scan(monkeypatch, [(a, b)])
    assert conflicts.scan() == []   # supersession/dedup territory, not disputes


def test_hallucinated_ids_are_dropped(monkeypatch):
    a = _fact_as("owner", "Some fact")
    _mock_scan(monkeypatch, [(a, "ffffffff"), (a, a)])
    assert conflicts.scan() == []


def test_apply_produces_the_standard_dispute_state(monkeypatch):
    monkeypatch.setattr(engine, "_subject_owner_for", lambda target: "owner")
    a = _fact_as("owner", "Kiaan's bus leaves at 8:15")
    b = _fact_as("ruma", "Kiaan's bus leaves at 8:40")
    _mock_scan(monkeypatch, [(a, b)])
    assert conflicts.apply(conflicts.scan()) == 1
    by_id = {e["id"]: e for e in engine._load()}
    assert by_id[a]["active"] and by_id[b]["active"]     # neither wins
    assert "disputed" in by_id[a]["flags"] and "disputed" in by_id[b]["flags"]
    notice = next(memory_store.PENDING_DIR.glob("*dispute*"))
    # and the EXISTING resolution semantics settle it
    outcome = memory_store.resolve_dispute(notice, keep_new=False)
    assert "discarded" in outcome
    by_id = {e["id"]: e for e in engine._load()}
    assert by_id[a]["active"] and not by_id[b]["active"]
    assert "disputed" not in by_id[a]["flags"]


def test_already_disputed_pairs_are_not_refiled(monkeypatch):
    monkeypatch.setattr(engine, "_subject_owner_for", lambda target: "owner")
    a = _fact_as("owner", "Nap time is 1pm")
    b = _fact_as("ruma", "Nap time is 3pm")
    _mock_scan(monkeypatch, [(a, b)])
    conflicts.apply(conflicts.scan())
    assert conflicts.scan() == []   # second scan: flagged pair skipped
    assert len(list(memory_store.PENDING_DIR.glob("*dispute*"))) == 1


def test_scan_failure_is_contained(monkeypatch):
    from kyraan.model_router import router

    def boom(**kw):
        raise RuntimeError("cloud down")

    monkeypatch.setattr(router, "call", boom)
    assert conflicts.nightly_scan() == 0


def test_notice_routing_prefers_an_enrolled_subject(monkeypatch):
    from kyraan.store import pg

    class _Conn:
        def __init__(self, stage):
            self._stage = stage

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            class R:
                def fetchone(inner):
                    return (self._stage,) if self._stage else None
            return R()

    monkeypatch.setattr(pg, "connection", lambda: _Conn("read_mostly"))
    assert engine._subject_owner_for("people/ruma.md") == "ruma"
    monkeypatch.setattr(pg, "connection", lambda: _Conn("none"))
    assert engine._subject_owner_for("people/ruma.md") == "owner"  # black-hole guard
