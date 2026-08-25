"""Proactive trigger layer — Phase 1 scope is reminders only.

Actual timer scheduling is delegated to the channel (Telegram's JobQueue)
via init(), so everything runs on one event loop instead of bridging a
background-thread scheduler into asyncio. Every fire checks
kernel.can_send_proactively() (kill switch + DND) before sending; a
reminder blocked by DND is rescheduled 15 minutes out instead of dropped.
"""
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from kyraan.control_plane import kernel
from kyraan.control_plane.logging_setup import log_event
from kyraan.triggers import store

ScheduleFn = Callable[[str, datetime, dict], None]
CancelFn = Callable[[str], None]
SendFn = Callable[[int, str], Awaitable[None]]

_schedule_fn: ScheduleFn | None = None
_cancel_fn: CancelFn | None = None
_send_fn: SendFn | None = None


def init(schedule_fn: ScheduleFn, cancel_fn: CancelFn, send_fn: SendFn) -> None:
    global _schedule_fn, _cancel_fn, _send_fn
    _schedule_fn, _cancel_fn, _send_fn = schedule_fn, cancel_fn, send_fn
    for reminder in store.list_pending():
        _schedule(reminder)


async def fire(reminder_id: str, chat_id: int, text: str) -> None:
    if not kernel.can_send_proactively():
        assert _schedule_fn is not None
        _schedule_fn(
            reminder_id,
            datetime.now().astimezone() + timedelta(minutes=15),
            {"chat_id": chat_id, "text": text, "reminder_id": reminder_id},
        )
        return
    assert _send_fn is not None, "scheduler.init() must be called before reminders can fire"
    await _send_fn(chat_id, f"Reminder: {text}")
    store.mark_sent(reminder_id)
    log_event("reminder_sent", reminder_id=reminder_id, chat_id=chat_id)


def _schedule(reminder: store.Reminder) -> None:
    assert _schedule_fn is not None, "scheduler.init() must be called before creating reminders"
    _schedule_fn(
        reminder.id,
        datetime.fromisoformat(reminder.when_iso),
        {"chat_id": reminder.chat_id, "text": reminder.text, "reminder_id": reminder.id},
    )


def create_reminder(chat_id: int, text: str, when_iso: str) -> store.Reminder:
    reminder = store.add(chat_id=chat_id, text=text, when_iso=when_iso)
    _schedule(reminder)
    log_event("reminder_created", reminder_id=reminder.id, chat_id=chat_id, when=when_iso)
    return reminder


def cancel_reminder(reminder_id: str) -> bool:
    if _cancel_fn:
        _cancel_fn(reminder_id)
    return store.cancel(reminder_id)
