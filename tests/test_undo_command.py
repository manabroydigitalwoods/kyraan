"""P3.1c — the deterministic `undo` command: ask wording, byte-identical
yes-path replay, no-path, empty log, head honesty, targeted reach, and
the PG-down honesty path. The action store is faked — the real store is
covered under the pg marker in test_store_actions.py."""
import itertools
from datetime import datetime, timezone

import pytest

from kyraan.agents import orchestrator
from kyraan.store import actions
from kyraan.store.actions import Action

_ids = itertools.count(910_000)


@pytest.fixture
def chat_id():
    return next(_ids)


def _action(tool="calendar.create_event", args=None, undo_tool="calendar.delete_event",
            undo_args=None, undone=False):
    return Action(
        id="a1", chat_id=1, tool=tool,
        args=args if args is not None else {"title": "lunch Friday 1pm"},
        undo_tool=undo_tool,
        undo_args=(undo_args if undo_args is not None
                   else {"event_id": "ev1", "title": "lunch Friday 1pm"}),
        done_at=datetime.now(timezone.utc),
        undone_at=datetime.now(timezone.utc) if undone else None)


async def test_undo_ask_names_the_inverse_concretely(monkeypatch, chat_id):
    monkeypatch.setattr(actions, "last_action", lambda c: _action())
    reply = await orchestrator._dispatch(chat_id, "undo")
    assert 'delete the event "lunch Friday 1pm"' in reply
    assert 'reply "yes"' in reply


async def test_yes_executes_the_stashed_inverse_byte_identically(monkeypatch, chat_id):
    monkeypatch.setattr(actions, "last_action", lambda c: _action())
    undone = []
    monkeypatch.setattr(actions, "mark_undone", lambda aid: undone.append(aid))
    executed = []

    async def fake_run_tool(call, *a, **k):
        executed.append((call.tool_name, dict(call.args)))
        return {"id": "ev1", "deleted": True}

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)
    await orchestrator._dispatch(chat_id, "undo")
    reply = await orchestrator._dispatch(chat_id, "yes")
    assert executed == [("calendar.delete_event",
                         {"event_id": "ev1", "title": "lunch Friday 1pm"})]
    assert undone == ["a1"]
    assert "undone" in reply.lower()


async def test_no_cancels_without_executing(monkeypatch, chat_id):
    monkeypatch.setattr(actions, "last_action", lambda c: _action())
    executed = []
    monkeypatch.setattr(orchestrator.kernel, "run_tool",
                        lambda *a, **k: executed.append(a))
    await orchestrator._dispatch(chat_id, "undo")
    reply = await orchestrator._dispatch(chat_id, "no")
    assert executed == []
    assert "cancelled" in reply.lower() and "nothing was done" in reply.lower()


async def test_empty_log_is_honest(monkeypatch, chat_id):
    monkeypatch.setattr(actions, "last_action", lambda c: None)
    reply = await orchestrator._dispatch(chat_id, "undo")
    assert "Nothing to undo" in reply


async def test_irreversible_head_is_named_never_skipped(monkeypatch, chat_id):
    head = _action(tool="calendar.delete_event", args={"event_id": "e9"},
                   undo_tool=None, undo_args=None)
    reminder = _action(tool="reminders.create", args={"text": "call mom"},
                       undo_tool="reminders.cancel", undo_args={"reminder_id": "r1"})
    monkeypatch.setattr(actions, "last_action", lambda c: head)
    monkeypatch.setattr(actions, "last_undoable", lambda c: reminder)
    reply = await orchestrator._dispatch(chat_id, "undo")
    assert "can't be undone" in reply and "calendar.delete_event" in reply
    assert 'undo the reminder' in reply  # the targeted reach is offered


async def test_targeted_undo_reaches_the_named_kind(monkeypatch, chat_id):
    asked = []

    def last_of(c, prefix):
        asked.append(prefix)
        return _action(tool="reminders.create", args={"text": "call mom"},
                       undo_tool="reminders.cancel", undo_args={"reminder_id": "r1"})

    monkeypatch.setattr(actions, "last_action_of", last_of)
    reply = await orchestrator._dispatch(chat_id, "undo the reminder")
    assert asked == ["reminders."]
    assert 'cancel the reminder "call mom"' in reply


async def test_store_down_degrades_honestly(monkeypatch, chat_id):
    def boom(c):
        raise RuntimeError("pool timeout")

    monkeypatch.setattr(actions, "last_action", boom)
    reply = await orchestrator._dispatch(chat_id, "undo")
    assert "undo isn't available" in reply


async def test_failed_inverse_keeps_the_action_on_the_log(monkeypatch, chat_id):
    monkeypatch.setattr(actions, "last_action", lambda c: _action())
    undone = []
    monkeypatch.setattr(actions, "mark_undone", lambda aid: undone.append(aid))

    async def failing_run_tool(call, *a, **k):
        raise orchestrator.kernel.ToolFailed("event already gone")

    monkeypatch.setattr(orchestrator.kernel, "run_tool", failing_run_tool)
    await orchestrator._dispatch(chat_id, "undo")
    reply = await orchestrator._dispatch(chat_id, "yes")
    assert "Couldn't undo" in reply
    assert undone == []
