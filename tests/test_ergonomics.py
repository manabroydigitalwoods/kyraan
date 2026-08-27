"""Owner gap list (2026-08-27): reminder snooze/reschedule, calendar
move, document list/forget, and the known-but-unenrolled courtesy."""
from datetime import datetime, timedelta, timezone

import pytest

from kyraan.triggers import scheduler, store


@pytest.fixture
def sched(monkeypatch):
    calls = {"scheduled": [], "cancelled": []}
    scheduler.init(
        schedule_fn=lambda rid, when, payload: calls["scheduled"].append((rid, when)),
        cancel_fn=lambda rid: calls["cancelled"].append(rid),
        send_fn=None)
    return calls


def test_reschedule_moves_in_place(sched):
    r = scheduler.create_reminder(7, "call mom", "2027-01-01T21:00:00+05:30")
    moved, prior = scheduler.reschedule_reminder(7, r.id[:8], "2027-01-01T20:30:00+05:30")
    assert moved.id == r.id                       # same reminder, same id
    assert prior == "2027-01-01T21:00:00+05:30"
    assert "20:30" in moved.when_iso
    assert sched["cancelled"] == [r.id]           # old job cancelled, new set


def test_reschedule_unknown_id_is_honest(sched):
    with pytest.raises(ValueError, match="no pending reminder"):
        scheduler.reschedule_reminder(7, "zzzz", "2027-01-01T20:00:00+05:30")


def test_snooze_pending_moves_it(sched):
    r = scheduler.create_reminder(7, "water", "2027-01-01T21:00:00+05:30")
    moved, mode, prior = scheduler.snooze_reminder(7, 15, r.id[:8])
    assert mode == "moved" and moved.id == r.id and prior is not None


def test_snooze_recent_delivery_clones(sched):
    r = scheduler.create_reminder(7, "medicine", "2027-01-01T09:00:00+05:30")
    store.claim_for_send(r.id)
    store.mark_sent(r.id)                          # just delivered
    clone, mode, _ = scheduler.snooze_reminder(7, 10)
    assert mode == "cloned" and clone.id != r.id
    assert clone.text == "medicine"
    assert [p.id for p in store.list_pending(7)] == [clone.id]


def test_snooze_with_nothing_recent_is_honest(sched):
    with pytest.raises(ValueError, match="nothing was delivered"):
        scheduler.snooze_reminder(7, 10)


def test_snooze_bounds(sched):
    with pytest.raises(ValueError, match="between 1 minute"):
        scheduler.snooze_reminder(7, 0)


def test_snooze_recurring_leaves_the_series(sched):
    r = scheduler.create_reminder(7, "stretch", "2027-01-01T10:00:00+05:30",
                                  repeat="daily")
    store.claim_for_send(r.id)                     # delivered occurrence
    clone, mode, _ = scheduler.snooze_reminder(7, 5)
    assert mode == "cloned"
    kinds = {(p.text, p.repeat) for p in store.list_pending(7)}
    assert ("stretch", "daily") in kinds           # series untouched
    assert ("stretch", "") in kinds                # plus the one-shot echo


# --- calendar move: listing proof + undo shape ----------------------------

async def test_calendar_reschedule_requires_a_current_listing():
    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel
    with pytest.raises(kernel.ToolFailed, match="CURRENT listing"):
        await loop_tools._calendar_reschedule(
            7, {"event_id": "nope", "start": "2027-01-01T14:00:00+05:30",
                "end": "2027-01-01T15:00:00+05:30"}, "move lunch to 2pm")


def test_calendar_reschedule_undo_restores_prior_times():
    from kyraan.agents.loop_tools import UNDO_MAP
    prior = {"id": "ev1", "title": "lunch", "start": "2027-01-01T13:00:00+05:30",
             "end": "2027-01-01T14:00:00+05:30"}
    undo = UNDO_MAP["calendar.reschedule"](
        {"event_id": "ev1"}, {"id": "ev1"}, prior)
    assert undo == ("calendar.update_event",
                    {"event_id": "ev1", "title": "lunch",
                     "start": "2027-01-01T13:00:00+05:30",
                     "end": "2027-01-01T14:00:00+05:30"})
    assert UNDO_MAP["calendar.reschedule"]({"event_id": "e"}, {}, None) is None


def test_reminder_move_undo_shapes():
    from kyraan.agents.loop_tools import UNDO_MAP
    assert UNDO_MAP["reminders.snooze"](
        {}, {"mode": "cloned", "id": "abc12345"}, None
    ) == ("reminders.cancel", {"reminder_id": "abc12345"})
    assert UNDO_MAP["reminders.reschedule"](
        {}, {"id": "abc12345", "prior_when": "2027-01-01T21:00:00+05:30"}, None
    ) == ("reminders.reschedule",
          {"reminder_id": "abc12345", "when_iso": "2027-01-01T21:00:00+05:30"})


# --- forget-document flow --------------------------------------------------

async def test_forget_document_confirms_then_deletes(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.store import documents
    monkeypatch.setattr(documents, "search", lambda c, q, k=3: [
        {"doc_id": "d1", "caption": "ac repair card", "date": "2026-08-27",
         "text": "..."}])
    deleted = []
    monkeypatch.setattr(documents, "delete_documents",
                        lambda c, ids: deleted.append(ids) or ["ac repair card"])
    ask = await orchestrator._dispatch(970_001, "forget the card ac repair")
    assert 'DELETE 1 saved document(s): "ac repair card"' in ask
    reply = await orchestrator._dispatch(970_001, "yes")
    assert deleted == [["d1"]]
    assert "Deleted from document memory" in reply


async def test_forget_document_no_match_is_honest(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.store import documents
    monkeypatch.setattr(documents, "search", lambda c, q, k=3: [])
    reply = await orchestrator._dispatch(970_002, "forget the document unicorn")
    assert "nothing to forget" in reply.lower()