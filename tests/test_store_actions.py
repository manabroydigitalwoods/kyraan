"""P3.1a — action_log module against the real container. Marked `pg`.

Uses a throwaway chat_id per test so parallel runs and leftover rows
never collide; rows are deleted in teardown.
"""
import os
import random
from pathlib import Path

import pytest

_env = Path(__file__).resolve().parents[1] / ".env"
if "KYRAAN_PG_DSN" not in os.environ and _env.exists():
    for line in _env.read_text().splitlines():
        if line.startswith("KYRAAN_PG_DSN="):
            os.environ["KYRAAN_PG_DSN"] = line.split("=", 1)[1].strip()
            break

from kyraan.store import pg  # noqa: E402

pytestmark = pytest.mark.pg
if not pg.available():
    pytestmark = [pytest.mark.pg,
                  pytest.mark.skip(reason="local Postgres container unreachable")]
else:
    from kyraan.store import actions


@pytest.fixture
def chat_id():
    cid = -random.randrange(10**9, 10**10)  # negative: never a real chat
    yield cid
    with pg.connection() as conn:
        conn.execute("DELETE FROM action_log WHERE chat_id = %s", (cid,))
        conn.commit()


def test_record_and_last_action_round_trip(chat_id):
    aid = actions.record(chat_id, "reminders.create", {"text": "call mom"},
                         "reminders.cancel", {"reminder_id": "r1"})
    got = actions.last_action(chat_id)
    assert got is not None and got.id == aid
    assert got.tool == "reminders.create"
    assert got.args == {"text": "call mom"}
    assert got.undo_tool == "reminders.cancel"
    assert got.undo_args == {"reminder_id": "r1"}
    assert got.undoable


def test_irreversible_head_returned_as_is_not_skipped(chat_id):
    actions.record(chat_id, "reminders.create", {"text": "a"},
                   "reminders.cancel", {"reminder_id": "r1"})
    actions.record(chat_id, "calendar.delete_event", {"event_id": "e9"},
                   None, None)  # irreversible, and it is the NEWEST
    head = actions.last_action(chat_id)
    assert head.tool == "calendar.delete_event"
    assert not head.undoable  # undo must report this, not reach past it


def test_targeted_lookup_reaches_past_head_explicitly(chat_id):
    actions.record(chat_id, "reminders.create", {"text": "a"},
                   "reminders.cancel", {"reminder_id": "r1"})
    actions.record(chat_id, "calendar.create_event", {"title": "lunch"},
                   "calendar.delete_event", {"event_id": "e2"})
    got = actions.last_action_of(chat_id, "reminders.")
    assert got is not None and got.tool == "reminders.create"


def test_mark_undone_removes_from_head(chat_id):
    aid = actions.record(chat_id, "faces.remember", {"name": "X"},
                         "faces.forget", {"name": "X"})
    actions.mark_undone(aid)
    assert actions.last_action(chat_id) is None


def test_empty_log_returns_none(chat_id):
    assert actions.last_action(chat_id) is None
    assert actions.last_action_of(chat_id, "reminders.") is None
