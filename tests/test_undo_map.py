"""P3.1b — writes declare their inverses. The builders are pure
functions; record_action is tested with a monkeypatched store so no
Postgres is needed here (the store itself is covered in
test_store_actions.py under the pg marker)."""
import asyncio

import pytest

from kyraan.agents import loop_tools
from kyraan.agents.loop_tools import SKIP, UNDO_MAP


def test_calendar_create_declares_delete_with_executed_id():
    undo = UNDO_MAP["calendar.create_event"](
        {"title": "lunch", "start": "2026-08-28T13:00:00+05:30"},
        {"id": "ev123", "link": "http://x", "title": "lunch"}, None)
    assert undo == ("calendar.delete_event", {"event_id": "ev123", "title": "lunch"})


def test_calendar_create_without_id_is_not_undoable():
    assert UNDO_MAP["calendar.create_event"]({"title": "x"}, {"error": "boom"}, None) is None


def test_reminder_create_declares_cancel():
    undo = UNDO_MAP["reminders.create"](
        {"text": "call mom", "when_iso": "2026-08-28T09:00:00+05:30"},
        {"created": True, "id": "ab12cd34", "text": "call mom"}, None)
    assert undo == ("reminders.cancel", {"reminder_id": "ab12cd34"})


def test_duplicate_reminder_is_skipped_not_logged():
    undo = UNDO_MAP["reminders.create"](
        {"text": "call mom"}, {"__direct_reply__": "Already set: ..."}, None)
    assert undo is SKIP


def test_task_schedule_declares_cancel():
    undo = UNDO_MAP["tasks.schedule"](
        {"instruction": "check email"}, {"scheduled": True, "id": "t9"}, None)
    assert undo == ("tasks.cancel", {"task_id": "t9"})


def test_faces_remember_declares_forget_by_name():
    undo = UNDO_MAP["faces.remember"](
        {"name": "Suman Ghosh"}, {"__direct_reply__": "Saved..."}, None)
    assert undo == ("faces.forget", {"name": "Suman Ghosh"})


def test_home_switch_undo_restores_observed_prior_state():
    prior = {"entity": "switch.ac", "state": "off", "_tool": "home.turn_on"}
    undo = UNDO_MAP["home.turn_on"]({"entity": "switch.ac"}, {"ok": True}, prior)
    assert undo == ("home.turn_off", {"entity": "switch.ac"})


def test_home_switch_already_in_state_is_skipped():
    prior = {"entity": "switch.ac", "state": "on", "_tool": "home.turn_on"}
    assert UNDO_MAP["home.turn_on"]({"entity": "switch.ac"}, {"ok": True}, prior) is SKIP


def test_home_switch_unobserved_prior_is_not_undoable():
    # HA read failed before the write: never assume the opposite state.
    assert UNDO_MAP["home.turn_off"]({"entity": "switch.ac"}, {"ok": True}, None) is None


@pytest.mark.parametrize("tool", ["calendar.delete_event", "reminders.cancel",
                                  "tasks.cancel", "memory.forget"])
def test_irreversible_writes_log_explicit_none(tool):
    assert UNDO_MAP[tool]({"x": 1}, {"ok": True}, None) is None


def test_every_write_tool_in_menu_has_an_undo_entry():
    reads = loop_tools._READ_ONLY_TOOLS
    writes = set(loop_tools.TOOLS) - set(reads)
    missing = {t for t in writes if t not in UNDO_MAP}
    assert not missing, f"writes with no declared inverse policy: {missing}"


def test_record_action_writes_row_with_inverse(monkeypatch):
    logged = {}

    def fake_record(chat_id, tool, args, undo_tool, undo_args):
        logged.update(chat_id=chat_id, tool=tool, args=args,
                      undo_tool=undo_tool, undo_args=undo_args)
        return "aid"

    from kyraan.store import actions
    monkeypatch.setattr(actions, "record", fake_record)
    asyncio.run(loop_tools.record_action(
        7, "reminders.create", {"text": "x", "when_iso": "w"},
        {"created": True, "id": "r1"}, None))
    assert logged["undo_tool"] == "reminders.cancel"
    assert logged["undo_args"] == {"reminder_id": "r1"}


def test_record_action_skip_writes_nothing(monkeypatch):
    from kyraan.store import actions
    called = []
    monkeypatch.setattr(actions, "record", lambda *a, **k: called.append(a))
    asyncio.run(loop_tools.record_action(
        7, "reminders.create", {"text": "x"}, {"__direct_reply__": "dup"}, None))
    assert called == []


def test_record_action_store_failure_never_raises(monkeypatch):
    from kyraan.store import actions

    def boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(actions, "record", boom)
    asyncio.run(loop_tools.record_action(  # must not raise
        7, "tasks.schedule", {"instruction": "i"}, {"id": "t"}, None))
