"""Whether Postgres is currently trustworthy as a READ source.

Files are the write authority (arch §2.1); Postgres is a mirror. The
mirror defers on failure and never blocks the file write — which is
right — but it means PG can be silently BEHIND the files, and a PG-backed
read then serves stale truth. Two live-shaped failures come from exactly
that:

- a forgotten fact whose deactivation never reached PG reappears in
  answers once PG is read again (a privacy promise broken by an outage);
- a reminder created during an outage is missing from PG, so `get()`
  returns None and the fire path logs "record gone (cancelled?)" and
  skips it forever (a promise silently dropped).

So: any deferred mirror marks that store STALE, and while a store is
stale its PG reads are refused (callers fall back to the files, which
are never wrong). Only a successful FULL resync clears the mark.

The mark is persisted because the failure outlives the process: a
restart with an in-memory-only flag would resume trusting a mirror that
was never repaired.
"""
import json

from pathlib import Path

from kyraan.control_plane.filelock import atomic_write_text
from kyraan.control_plane.logging_setup import log_event

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
STATE_PATH = DATA_DIR / "pg_sync_state.json"


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def mark_stale(store: str, reason: str = "") -> None:
    """Record that `store`'s PG mirror is behind the files."""
    state = _load()
    if state.get(store, {}).get("stale"):
        return  # already known-stale; keep the FIRST reason
    state[store] = {"stale": True, "reason": reason[:200]}
    try:
        atomic_write_text(STATE_PATH, json.dumps(state, indent=2))
    except OSError as exc:  # disk trouble must not break the file write
        log_event("pg_sync_state_write_failed", store=store, error=str(exc)[:120])
        return
    log_event("pg_mirror_stale", store=store, reason=reason[:200])


def is_stale(store: str) -> bool:
    return bool(_load().get(store, {}).get("stale"))


def clear_stale(store: str) -> None:
    """Called ONLY after a full resync — an incremental mirror landing
    does not prove the entries missed while stale ever arrived."""
    state = _load()
    if not state.get(store, {}).get("stale"):
        return
    state.pop(store, None)
    try:
        atomic_write_text(STATE_PATH, json.dumps(state, indent=2))
    except OSError as exc:
        log_event("pg_sync_state_write_failed", store=store, error=str(exc)[:120])
        return
    log_event("pg_mirror_resynced", store=store)


def stale_stores() -> list:
    return sorted(k for k, v in _load().items() if v.get("stale"))
