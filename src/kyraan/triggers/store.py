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


def _load_all() -> list[dict]:
    if not REMINDERS_PATH.exists():
        return []
    return json.loads(REMINDERS_PATH.read_text())


def _save_all(records: list[dict]) -> None:
    atomic_write_text(REMINDERS_PATH, json.dumps(records, indent=2))


def add(chat_id: int, text: str, when_iso: str) -> Reminder:
    with locked(REMINDERS_PATH):
        return _add_locked(chat_id, text, when_iso)


def _add_locked(chat_id: int, text: str, when_iso: str) -> Reminder:
    reminder = Reminder(id=str(uuid.uuid4()), chat_id=chat_id, text=text, when_iso=when_iso)
    records = _load_all()
    records.append(asdict(reminder))
    _save_all(records)
    return reminder


def list_pending(chat_id: int | None = None) -> list[Reminder]:
    records = [r for r in _load_all() if not r["sent"]]
    if chat_id is not None:
        records = [r for r in records if r["chat_id"] == chat_id]
    return [Reminder(**r) for r in records]


def get(reminder_id: str) -> Reminder | None:
    return next((Reminder(**r) for r in _load_all() if r["id"] == reminder_id), None)


def mark_sent(reminder_id: str) -> None:
    with locked(REMINDERS_PATH):
        _mark_sent_locked(reminder_id)


def _mark_sent_locked(reminder_id: str) -> None:
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
            if claimed:
                try:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(claimed)
                    if age < timedelta(seconds=lease_seconds):
                        return False
                except ValueError:
                    pass
            record["claimed_at"] = datetime.now(timezone.utc).isoformat()
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
