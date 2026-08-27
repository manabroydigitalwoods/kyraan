"""Promise mirroring: reminders/agent-tasks/cost-ledger JSON → Postgres
(P3.2d, arch §2.2).

Same doctrine as facts (P3.2a): the JSON files remain the write
authority; every file save mirrors the FULL store state here (the
stores are small — full-state sync is what makes cancel-as-removal and
read-modify-write mutations trivially correct). PG failures log
`promise_sync_deferred` behind a shared 60s breaker and never block the
file op.

Reads flip per store with KYRAAN_PROMISES_BACKEND=files|pg (default
files): pg mode serves the load_* functions from the tables, and any
failure falls back to the caller's file read with one logged event.
`pg_claim_for_send` implements the delivery lease ATOMICALLY in SQL —
the crash-window semantics the cutover needs, proven by the pg tests
before any flag flip.
"""
import time

from kyraan.control_plane.logging_setup import log_event
from kyraan.store import pg

MIRROR_ENABLED = True  # conftest flips this off suite-wide (like facts)

_BREAKER_S = 60
_breaker_until = 0.0

_REMINDER_FIELDS = ("id", "chat_id", "text", "when_iso", "sent", "claimed_at",
                    "takeover", "repeat", "interval_minutes", "window_start",
                    "window_end")
_TASK_FIELDS = ("id", "chat_id", "instruction", "when_iso", "repeat",
                "active", "pending_result")
_DEFAULTS = {"sent": False, "claimed_at": "", "takeover": False, "repeat": "",
             "interval_minutes": 0, "window_start": "", "window_end": "",
             "active": True, "pending_result": ""}


def backend() -> str:
    import os
    return os.environ.get("KYRAAN_PROMISES_BACKEND", "files").strip().lower()


def _guarded_mirror(kind: str, sync) -> bool:
    global _breaker_until
    if not MIRROR_ENABLED:
        return False
    if time.monotonic() < _breaker_until:
        log_event("promise_sync_deferred", store=kind, reason="breaker open")
        return False
    try:
        with pg.connection() as conn:
            sync(conn)
            conn.commit()
        return True
    except Exception as exc:
        _breaker_until = time.monotonic() + _BREAKER_S
        log_event("promise_sync_deferred", store=kind, reason=str(exc)[:200])
        return False


def _sync_table(conn, table: str, fields: tuple, records: list) -> None:
    """Full-state sync: upsert every record, delete rows the file no
    longer holds (cancel is removal in both JSON stores)."""
    ids = [str(r["id"]) for r in records]
    if ids:
        conn.execute(f"DELETE FROM {table} WHERE id != ALL(%s)", (ids,))  # noqa: S608
    else:
        conn.execute(f"DELETE FROM {table}")  # noqa: S608
    cols = ", ".join(fields)
    holders = ", ".join(["%s"] * len(fields))
    updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in fields if f != "id")
    for r in records:
        conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({holders}) "  # noqa: S608
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            tuple(r.get(f, _DEFAULTS.get(f)) for f in fields))


def mirror_reminders(records: list) -> bool:
    return _guarded_mirror(
        "reminders", lambda c: _sync_table(c, "reminder", _REMINDER_FIELDS, records))


def mirror_tasks(records: list) -> bool:
    return _guarded_mirror(
        "tasks", lambda c: _sync_table(c, "agent_task", _TASK_FIELDS, records))


def mirror_ledger(ledger: dict) -> bool:
    import json as _json

    def sync(conn):
        keys = list(ledger)
        if keys:
            conn.execute("DELETE FROM cost_ledger WHERE key != ALL(%s)", (keys,))
        else:
            conn.execute("DELETE FROM cost_ledger")
        for key, value in ledger.items():
            conn.execute(
                """INSERT INTO cost_ledger (key, value) VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                (key, _json.dumps(value)))

    return _guarded_mirror("ledger", sync)


# --- pg reads (flag=pg) ---------------------------------------------------

def _rows_to_dicts(rows, fields) -> list:
    return [dict(zip(fields, r)) for r in rows]


def load_reminders() -> list | None:
    """All reminder records as file-shaped dicts, or None on failure (the
    caller falls back to the file and logs)."""
    try:
        with pg.connection() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_REMINDER_FIELDS)} FROM reminder").fetchall()
        return _rows_to_dicts(rows, _REMINDER_FIELDS)
    except Exception as exc:
        log_event("promises_backend_fallback", store="reminders",
                  reason=str(exc)[:200])
        return None


def load_tasks() -> list | None:
    try:
        with pg.connection() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_TASK_FIELDS)} FROM agent_task").fetchall()
        return _rows_to_dicts(rows, _TASK_FIELDS)
    except Exception as exc:
        log_event("promises_backend_fallback", store="tasks",
                  reason=str(exc)[:200])
        return None


def load_ledger() -> dict | None:
    try:
        with pg.connection() as conn:
            rows = conn.execute("SELECT key, value FROM cost_ledger").fetchall()
        return {k: v for k, v in rows}
    except Exception as exc:
        log_event("promises_backend_fallback", store="ledger",
                  reason=str(exc)[:200])
        return None


# --- the delivery lease, atomically in SQL --------------------------------

def pg_claim_for_send(reminder_id: str, lease_seconds: int = 120) -> bool:
    """The crash-window claim on pg: one UPDATE decides — unsent, and
    either unclaimed or the claim is a stale (crashed) lease. Takeover
    stays STICKY exactly like the file store: once delivery uncertainty
    exists only confirmed success clears it."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(seconds=lease_seconds)).isoformat()
    with pg.connection() as conn:
        row = conn.execute(
            """UPDATE reminder
               SET takeover = (claimed_at <> '') OR takeover,
                   claimed_at = %s
               WHERE id = %s AND NOT sent
                     AND (claimed_at = '' OR claimed_at < %s)
               RETURNING id""",
            (now.isoformat(), reminder_id, stale_before)).fetchone()
        conn.commit()
    return row is not None
