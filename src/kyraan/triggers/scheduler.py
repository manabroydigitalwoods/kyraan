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
from kyraan.control_plane.dnd import local_now
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
        try:
            _schedule(reminder)
        except ValueError as exc:
            # A persisted record with an unparseable when_iso must not take
            # the whole app down on every future startup — log it and skip
            # rather than crash init() (which runs on every mount/launch).
            log_event("reminder_schedule_failed", reminder_id=reminder.id, when_iso=reminder.when_iso, error=str(exc))


async def fire(reminder_id: str, chat_id: int, text: str) -> None:
    # Idempotency guard — the store is the single source of truth on
    # whether this reminder still owes a send. Found live (2026-08-25): a
    # reminder fired twice, 0.8s apart, when two bot processes briefly
    # overlapped across a restart — fire() trusted whoever scheduled it
    # and sent unconditionally. Now a duplicate job, an overlapping
    # process, or a stale schedule sends nothing the second time.
    record = store.get(reminder_id)
    if record is None or record.sent:
        log_event("reminder_fire_skipped", reminder_id=reminder_id,
                  reason="already sent" if record else "record gone (cancelled?)")
        return
    if not kernel.can_send_proactively():
        assert _schedule_fn is not None
        _schedule_fn(
            reminder_id,
            local_now() + timedelta(minutes=15),
            {"chat_id": chat_id, "text": text, "reminder_id": reminder_id},
        )
        return
    assert _send_fn is not None, "scheduler.init() must be called before reminders can fire"
    await _send_fn(chat_id, f"Reminder: {text}")
    store.mark_sent(reminder_id)
    log_event("reminder_sent", reminder_id=reminder_id, chat_id=chat_id)


def _parse_when(when_iso: str) -> datetime:
    """The extraction prompt asks the model for an offset-aware ISO datetime,
    but models sometimes drop the offset — if so, assume it meant
    KYRAAN_TIMEZONE (the same tz "now" was expressed in) rather than
    crashing or silently assuming UTC/system tz."""
    parsed = datetime.fromisoformat(when_iso)
    now = local_now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    elif (
        parsed.utcoffset().total_seconds() == 0
        and now.utcoffset() is not None
        and now.utcoffset().total_seconds() != 0
    ):
        # Model wrote "...Z" while the user lives in a non-UTC timezone —
        # seen live: "call suman at 7pm" came back 19:00:00.000Z, which
        # would have fired at 00:30 local, 5.5h late. For a personal
        # assistant a stated clock time is always wall-clock in the user's
        # tz; a Z here is the model dropping the offset, not the user
        # meaning UTC. Reinterpret the wall time as local.
        log_event("reminder_tz_reinterpreted", when_iso=when_iso)
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


# How far past its due time a reminder can be before it's treated as
# overdue rather than merely "just now". Absorbs scheduling jitter and the
# create->schedule gap without annotating a reminder that fired on time.
_OVERDUE_SLACK = timedelta(seconds=60)


def _schedule(reminder: store.Reminder) -> None:
    assert _schedule_fn is not None, "scheduler.init() must be called before creating reminders"
    when = _parse_when(reminder.when_iso)
    text = reminder.text
    if when < local_now() - _OVERDUE_SLACK:
        # A due time that already passed — the app was down when it should
        # have fired (or a model extracted a past datetime). What happens
        # to a past timestamp is the channel scheduler's undefined
        # behavior, so decide explicitly: fire once, now, and say it's
        # late rather than silently pretending it's on time or never
        # firing at all.
        log_event("reminder_overdue", reminder_id=reminder.id, when_iso=reminder.when_iso)
        text = f"{reminder.text} (was due {reminder.when_iso})"
        when = local_now()
    _schedule_fn(
        reminder.id,
        when,
        {"chat_id": reminder.chat_id, "text": text, "reminder_id": reminder.id},
    )


def create_reminder(chat_id: int, text: str, when_iso: str) -> store.Reminder:
    # Validate before persisting — a bad when_iso (e.g. a model producing a
    # duplicated UTC offset, seen live) must never be written to disk, or
    # it becomes a landmine that re-crashes init() on every future startup.
    _parse_when(when_iso)
    reminder = store.add(chat_id=chat_id, text=text, when_iso=when_iso)
    _schedule(reminder)
    log_event("reminder_created", reminder_id=reminder.id, chat_id=chat_id, when=when_iso)
    return reminder


def cancel_reminder(reminder_id: str) -> bool:
    if _cancel_fn:
        _cancel_fn(reminder_id)
    return store.cancel(reminder_id)
