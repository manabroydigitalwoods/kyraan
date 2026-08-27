"""P3.5d — cross-person disputes and per-person config. Done-when: a
seeded contradiction shows up in the RIGHT queue and neither fact
silently wins."""
from types import SimpleNamespace

import pytest

from kyraan.control_plane import kernel
from kyraan.memory import engine
from kyraan.memory import store as memory_store


def _as(person, stage="full"):
    return kernel.set_viewer(person, stage)


def _entry(fact_id):
    return next(e for e in engine._load() if e["id"] == fact_id)


def _seed_conflict(monkeypatch, subject_owner="owner"):
    """Owner states a fact; ruma later contradicts it."""
    monkeypatch.setattr(engine, "_subject_owner_for", lambda target: subject_owner)
    owner_fact = engine.add_fact("Kiaan's school starts at 9am",
                                 "people/kiaan.md", "owner said")
    token = _as("ruma")
    try:
        ruma_fact = engine.add_fact("Kiaan's school starts at 8am",
                                    "people/kiaan.md", "ruma said",
                                    supersedes="Kiaan's school starts at 9am")
    finally:
        kernel.reset_viewer_stage(token)
    return owner_fact, ruma_fact


def test_cross_person_contradiction_never_supersedes(monkeypatch):
    old_id, new_id = _seed_conflict(monkeypatch)
    old, new = _entry(old_id), _entry(new_id)
    assert old["active"] and new["active"]          # neither silently wins
    assert "disputed" in old["flags"] and "disputed" in new["flags"]
    assert old["superseded_by"] is None
    assert old["author"] == "owner" and new["author"] == "ruma"


def test_same_person_supersession_is_unchanged():
    a = engine.add_fact("Favourite tea is masala", "preferences/tea.md", "t")
    b = engine.add_fact("Favourite tea is green", "preferences/tea.md", "t",
                        supersedes="Favourite tea is masala")
    assert _entry(a)["active"] is False
    assert _entry(a)["superseded_by"] == b
    assert "disputed" not in _entry(b)["flags"]


def test_dispute_lands_in_the_subject_owners_queue(monkeypatch):
    from kyraan.agents.review import _load_review_proposals
    _seed_conflict(monkeypatch, subject_owner="kiaan")
    assert not _load_review_proposals("owner")
    assert not _load_review_proposals("ruma")
    queue = _load_review_proposals("kiaan")     # the SUBJECT-owner's queue
    assert len(queue) == 1
    assert "DISPUTED" in queue[0][2]


def test_approve_keeps_new_reject_keeps_old(monkeypatch):
    old_id, new_id = _seed_conflict(monkeypatch)
    path = next(memory_store.PENDING_DIR.glob("*dispute*"))
    outcome = memory_store.resolve_dispute(path, keep_new=True)
    assert "new claim stands" in outcome
    assert _entry(old_id)["active"] is False
    assert _entry(old_id)["superseded_by"] == new_id
    assert _entry(new_id)["active"] is True
    assert "disputed" not in _entry(new_id)["flags"]
    assert not path.exists()

    monkeypatch.setattr(engine, "_subject_owner_for", lambda target: "owner")
    old2 = engine.add_fact("Kiaan's bus leaves at 8:15",
                           "people/kiaan.md", "owner said")
    token = _as("ruma")
    try:
        new2 = engine.add_fact("Kiaan's bus leaves at 8:30",
                               "people/kiaan.md", "ruma said",
                               supersedes="Kiaan's bus leaves at 8:15")
    finally:
        kernel.reset_viewer_stage(token)
    path2 = next(memory_store.PENDING_DIR.glob("*dispute*"))
    memory_store.resolve_dispute(path2, keep_new=False)
    assert _entry(old2)["active"] is True
    assert _entry(new2)["active"] is False       # forgotten, kept as history
    assert "disputed" not in _entry(old2)["flags"]


def test_promote_refuses_a_dispute_notice(monkeypatch):
    _seed_conflict(monkeypatch)
    path = next(memory_store.PENDING_DIR.glob("*dispute*"))
    with pytest.raises(ValueError, match="DISPUTE notice"):
        memory_store.promote(path)


# --- per-person DND -------------------------------------------------------

def test_person_dnd_window_blocks_their_sends(monkeypatch):
    from kyraan.store import persons
    monkeypatch.setattr(persons, "dnd_window",
                        lambda chat_id: ("00:00", "23:59") if chat_id == 111 else None)
    monkeypatch.setattr(kernel.dnd, "in_quiet_hours", lambda: False)
    assert kernel.can_send_proactively(chat_id=111) is False   # her window
    assert kernel.can_send_proactively(chat_id=222) is True    # no window
    assert kernel.can_send_proactively(chat_id=None) is True   # owner path
    assert kernel.can_send_proactively(force=True, chat_id=111) is True


def test_person_window_wraps_midnight(monkeypatch):
    fake_now = SimpleNamespace(strftime=lambda fmt: "23:30")
    monkeypatch.setattr(kernel.dnd, "local_now", lambda: fake_now)
    assert kernel._in_person_window("22:00", "07:00") is True
    fake_now = SimpleNamespace(strftime=lambda fmt: "12:00")
    monkeypatch.setattr(kernel.dnd, "local_now", lambda: fake_now)
    assert kernel._in_person_window("22:00", "07:00") is False


# --- per-person budget ----------------------------------------------------

def test_person_budget_refuses_when_spent(monkeypatch):
    from kyraan.model_router import router
    from kyraan.store import persons
    monkeypatch.setattr(persons, "daily_budget", lambda p: 0.10)
    from kyraan.control_plane.dnd import local_now
    day = local_now().date().isoformat()
    monkeypatch.setattr(router, "_read_ledger",
                        lambda: {f"person:ruma:{day}": 0.11})
    token = _as("ruma")
    try:
        with pytest.raises(router.ModelProviderError, match="ruma's daily"):
            router.call(prompt="hi", tier="cheap")
    finally:
        kernel.reset_viewer_stage(token)


def test_owner_is_never_person_capped(monkeypatch):
    from kyraan.model_router import router
    from kyraan.store import persons

    def boom(p):
        raise AssertionError("owner turns must never hit the person cap")

    monkeypatch.setattr(persons, "daily_budget", boom)
    called = []
    monkeypatch.setattr(router, "_token_guard",
                        lambda *a, **k: called.append(1) or (_ for _ in ()).throw(
                            router.ModelProviderError("stop here")))
    with pytest.raises(router.ModelProviderError, match="stop here"):
        router.call(prompt="hi", tier="cheap")
    assert called  # got past the budget checks to the token guard