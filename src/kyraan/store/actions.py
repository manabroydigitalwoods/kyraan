"""action_log access — every side-effectful tool call lands here with
its declared inverse (or an explicit None for irreversible ones).

Undo semantics (arch v2, audit P1): `last_action` returns the NEWEST
action whether or not it is undoable — undo must never silently reach
past the head and reverse an older action than the one the user means.
Targeted forms ("undo the reminder") reach past it explicitly via
`last_action_of`.

PG down ⇒ these raise; callers degrade honestly ("can't undo right
now") rather than pretending nothing happened.
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from kyraan.store import pg

# Suite-wide kill switch, the same pattern facts.py and promises.py use.
# Without it every pytest run wrote real rows into the production
# action_log: found 2026-08-31 with 2,450 of 2,473 rows belonging to the
# test chat ids 90 and 93, against 33 real ones. The undo history is a
# safety surface — it has to be the owner's actions and nobody else's.
MIRROR_ENABLED = True


@dataclass(frozen=True)
class Action:
    id: str
    chat_id: int
    tool: str
    args: dict
    undo_tool: str | None
    undo_args: dict | None
    done_at: datetime
    undone_at: datetime | None

    @property
    def undoable(self) -> bool:
        return self.undo_tool is not None and self.undone_at is None


def record(chat_id: int, tool: str, args: dict,
           undo_tool: str | None, undo_args: dict | None) -> str:
    """Log one executed write. Returns the action id."""
    action_id = str(uuid.uuid4())
    if not MIRROR_ENABLED:
        return action_id
    with pg.connection() as conn:
        conn.execute(
            """INSERT INTO action_log
                   (id, chat_id, tool, args, undo_tool, undo_args, done_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (action_id, chat_id, tool, json.dumps(args), undo_tool,
             json.dumps(undo_args) if undo_args is not None else None,
             datetime.now(timezone.utc)))
        conn.commit()
    return action_id


_COLS = "id, chat_id, tool, args, undo_tool, undo_args, done_at, undone_at"


def _row_to_action(row) -> Action:
    return Action(
        id=str(row[0]), chat_id=row[1], tool=row[2],
        args=row[3] if isinstance(row[3], dict) else json.loads(row[3]),
        undo_tool=row[4],
        undo_args=(row[5] if isinstance(row[5], dict)
                   else json.loads(row[5]) if row[5] is not None else None),
        done_at=row[6], undone_at=row[7])


def last_action(chat_id: int) -> Action | None:
    """The newest not-yet-undone action — undoable or not."""
    with pg.connection() as conn:
        row = conn.execute(
            f"""SELECT {_COLS} FROM action_log
                WHERE chat_id = %s AND undone_at IS NULL
                ORDER BY done_at DESC LIMIT 1""", (chat_id,)).fetchone()
    return _row_to_action(row) if row else None


def last_action_of(chat_id: int, tool_prefix: str) -> Action | None:
    """Targeted reach-past ("undo the reminder"): newest matching action."""
    with pg.connection() as conn:
        row = conn.execute(
            f"""SELECT {_COLS} FROM action_log
                WHERE chat_id = %s AND undone_at IS NULL
                      AND tool LIKE %s || '%%'
                ORDER BY done_at DESC LIMIT 1""",
            (chat_id, tool_prefix)).fetchone()
    return _row_to_action(row) if row else None


def last_undoable(chat_id: int) -> Action | None:
    """The newest action that CAN be undone — only for naming what a
    targeted undo could still reach when the head is irreversible; the
    plain `undo` command must use last_action and never this."""
    with pg.connection() as conn:
        row = conn.execute(
            f"""SELECT {_COLS} FROM action_log
                WHERE chat_id = %s AND undone_at IS NULL
                      AND undo_tool IS NOT NULL
                ORDER BY done_at DESC LIMIT 1""", (chat_id,)).fetchone()
    return _row_to_action(row) if row else None


def mark_undone(action_id: str) -> None:
    with pg.connection() as conn:
        conn.execute(
            "UPDATE action_log SET undone_at = %s WHERE id = %s",
            (datetime.now(timezone.utc), action_id))
        conn.commit()
