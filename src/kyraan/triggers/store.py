"""Durable storage for reminders — plain JSON, reloaded into the scheduler
on startup so a restart doesn't lose pending reminders.
"""
import json

from kyraan.control_plane.filelock import atomic_write_text, locked
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
DATA_DIR.mkdir(exist_ok=True)
REMINDERS_PATH = DATA_DIR / "reminders.json"


@dataclass
class Reminder:
    id: str
    chat_id: int
    text: str
    when_iso: str
    sent: bool = False
    claimed_at: str = ""  # F4: set atomically before delivery; a stale
                          # claim (>120s) is a crashed sender's lease
    takeover: bool = False  # this claim took over a stale lease — the
                            # send may be a repeat and must say so
    repeat: str = ""        # "", daily, weekdays, weekly, monthly, or
                            # "interval" — recurring reminders roll
                            # when_iso forward after each delivery
    interval_minutes: int = 0   # for repeat="interval" (floor 15)
    window_start: str = ""      # "HH:MM" — interval reminders pause
    window_end: str = ""        # outside this daily window


def _load_all() -> list[dict]:
    if not REMINDERS_PATH.exists():
        return []
    return json.loads(REMINDERS_PATH.read_text())


def _save_all(records: list[dict]) -> None:
    atomic_write_text(REMINDERS_PATH, json.dumps(records, indent=2))
    # P3.2d: mirror the full store state to Postgres AFTER the file write
    # — file is authority; failures defer inside (never raise here).
    from kyraan.store import promises
    promises.mirror_reminders(records)


def _records_for_read() -> list[dict]:
    """Pure reads honor KYRAAN_PROMISES_BACKEND=pg (P3.2d); mutations
    always read-modify-write the FILE — the direction never reverses."""
    from kyraan.store import promises
    if promises.backend() == "pg":
        records = promises.load_reminders()
        if records is not None:
            return records
    return _load_all()


def add(chat_id: int, text: str, when_iso: str, repeat: str = "") -> Reminder:
    with locked(REMINDERS_PATH):
        return _add_locked(chat_id, text, when_iso, repeat)


def _add_locked(chat_id: int, text: str, when_iso: str, repeat: str = "") -> Reminder:
    reminder = Reminder(id=str(uuid.uuid4()), chat_id=chat_id, text=text,
                        when_iso=when_iso, repeat=repeat)
    records = _load_all()
    records.append(asdict(reminder))
    _save_all(records)
    return reminder


def list_pending(chat_id: int | None = None) -> list[Reminder]:
    records = [r for r in _records_for_read() if not r["sent"]]
    if chat_id is not None:
        records = [r for r in records if r["chat_id"] == chat_id]
    return [Reminder(**r) for r in records]


def get(reminder_id: str) -> Reminder | None:
    return next((Reminder(**r) for r in _records_for_read()
                 if r["id"] == reminder_id), None)


def mark_sent(reminder_id: str) -> None:
    with locked(REMINDERS_PATH):
        _mark_sent_locked(reminder_id)


def _mark_sent_locked(reminder_id: str) -> None:
    # the one place uncertainty legitimately ends
    records = _load_all()
    for r in records:
        if r["id"] == reminder_id:
            r["sent"] = True
    _save_all(records)


def claim_for_send(reminder_id: str, lease_seconds: int = 120) -> bool:
    """Atomically claim a reminder for delivery (external review P1: two
    overlapping jobs could both observe sent=False and both deliver).
    True = this caller owns the send; a live unexpired claim or an
    already-sent record returns False. A stale claim is a crashed
    sender's lease and may be taken over."""
    from datetime import datetime, timedelta, timezone

    with locked(REMINDERS_PATH):
        records = _load_all()
        for record in records:
            if record["id"] != reminder_id:
                continue
            if record.get("sent"):
                return False
            claimed = record.get("claimed_at") or ""
            takeover = False
            if claimed:
                try:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(claimed)
                    if age < timedelta(seconds=lease_seconds):
                        return False
                    takeover = True  # a prior attempt died mid-send
                except ValueError:
                    pass
            record["claimed_at"] = datetime.now(timezone.utc).isoformat()
            # STICKY (round-6 P1): once delivery uncertainty exists, only
            # confirmed success may clear it — a DND release and re-claim
            # must not launder a possible duplicate into a fresh send.
            record["takeover"] = takeover or bool(record.get("takeover"))
            _save_all(records)
            return True
        return False


def release_claim(reminder_id: str) -> None:
    with locked(REMINDERS_PATH):
        records = _load_all()
        for record in records:
            if record["id"] == reminder_id:
                record["claimed_at"] = ""
        _save_all(records)


def cancel(reminder_id: str) -> bool:
    with locked(REMINDERS_PATH):
        return _cancel_locked(reminder_id)


def _cancel_locked(reminder_id: str) -> bool:
    records = _load_all()
    remaining = [r for r in records if r["id"] != reminder_id]
    changed = len(remaining) != len(records)
    _save_all(remaining)
    return changed


def roll_forward(reminder_id: str, next_when_iso: str) -> None:
    """A recurring reminder just delivered: advance to its next
    occurrence, release the claim, and keep it un-retired — sent stays
    False so init() reschedules the NEXT instance after any restart."""
    with locked(REMINDERS_PATH):
        records = _load_all()
        for record in records:
            if record["id"] == reminder_id:
                record["when_iso"] = next_when_iso
                record["claimed_at"] = ""
                record["takeover"] = False
        _save_all(records)
