"""Proactive trigger layer — Phase 1 scope is reminders only.

Actual timer scheduling is delegated to the channel (Telegram's JobQueue)
via init(), so everything runs on one event loop instead of bridging a
background-thread scheduler into asyncio. Every fire checks
kernel.can_send_proactively() (kill switch + DND) before sending; a
reminder blocked by DND is rescheduled 15 minutes out instead of dropped.
"""
import re
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


def init(schedule_fn: ScheduleFn, cancel_fn: CancelFn, send_fn: SendFn,
         only_chat: int | None = None) -> None:
    """only_chat: dev harnesses pass their own chat id so they never
    schedule (and steal) the owner's real reminders — audit finding: a
    long-running TUI would have fired the owner's 9 PM reminder to its
    console, marked it sent, and the idempotency guard would then have
    suppressed the real Telegram delivery."""
    global _schedule_fn, _cancel_fn, _send_fn
    _schedule_fn, _cancel_fn, _send_fn = schedule_fn, cancel_fn, send_fn
    for reminder in store.list_pending(only_chat):
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
    # Atomic claim BEFORE anything external happens — two overlapping
    # jobs could both pass the sent-check above (P1, and the double-send
    # was observed live once before the check existed at all).
    if not store.claim_for_send(reminder_id):
        record = store.get(reminder_id)
        if record is not None and not record.sent:
            # A live lease with an unsent record is either a concurrent
            # sender (its success will mark sent, making our retry a
            # no-op) or a crashed one (review P1: the lease had no
            # watcher and the reminder stranded) — watch it either way.
            log_event("reminder_fire_deferred", reminder_id=reminder_id, reason="live claim lease")
            assert _schedule_fn is not None
            _schedule_fn(reminder_id, local_now() + timedelta(seconds=130),
                         {"chat_id": chat_id, "text": text, "reminder_id": reminder_id})
        else:
            log_event("reminder_fire_skipped", reminder_id=reminder_id, reason="already sent")
        return
    if not kernel.can_send_proactively(chat_id=chat_id):  # P3.5d: the
        # recipient's OWN quiet hours gate their reminders too
        store.release_claim(reminder_id)
        assert _schedule_fn is not None
        _schedule_fn(
            reminder_id,
            local_now() + timedelta(minutes=15),
            {"chat_id": chat_id, "text": text, "reminder_id": reminder_id},
        )
        return
    assert _send_fn is not None, "scheduler.init() must be called before reminders can fire"
    # send_fn reports delivery: False = deliberately withheld (e.g. the
    # channel's owner-only guard retiring a dev-harness record). The audit
    # log must say what actually happened — it previously logged
    # reminder_sent for a message that was never sent. None (legacy
    # send_fns) counts as delivered.
    # Exactly-once is impossible without a transactional external send: a
    # crash after Telegram accepts but before mark_sent leaves a stale
    # lease, and losing the reminder would break the product's core
    # promise. So delivery is at-least-once — and HONEST about it: a
    # stale-lease takeover knows a prior attempt was in flight and says so
    # (external review round 4, P1).
    record = store.get(reminder_id)
    suffix = (" (this may be a repeat — an earlier delivery attempt may have "
              "reached you)") if record is not None and getattr(record, "takeover", False) else ""
    try:
        delivered = await _send_fn(chat_id, f"Reminder: {text}{suffix}")
    except Exception as exc:
        # A client-side send exception is AMBIGUOUS — Telegram may have
        # accepted the message before the connection died (round-5 P1).
        # So the claim is deliberately NOT released: it goes stale, the
        # retry takes it over, and the stale-lease takeover applies the
        # "may be a repeat" label through the one mechanism built for
        # exactly this. Retry lands after the lease expires.
        log_event("reminder_send_failed", reminder_id=reminder_id, error=str(exc)[:200])
        assert _schedule_fn is not None
        _schedule_fn(reminder_id, local_now() + timedelta(seconds=150),
                     {"chat_id": chat_id, "text": text, "reminder_id": reminder_id})
        return
    if record.repeat:
        # Recurring: roll forward instead of retiring — the record IS the
        # series; when_iso always points at the next occurrence, so a
        # restart's init() reschedules the series naturally.
        next_when = advance_past_now(record)
        store.roll_forward(reminder_id, next_when.isoformat())
        assert _schedule_fn is not None
        _schedule_fn(reminder_id, next_when,
                     {"chat_id": chat_id, "text": record.text, "reminder_id": reminder_id})
        log_event("reminder_recurred", reminder_id=reminder_id,
                  next=next_when.isoformat(), repeat=record.repeat)
        return
    store.mark_sent(reminder_id)
    if delivered is False:
        log_event("reminder_retired_undelivered", reminder_id=reminder_id, chat_id=chat_id)
    else:
        log_event("reminder_sent", reminder_id=reminder_id, chat_id=chat_id)


def _sanitize_iso(value: str) -> str:
    """Deterministic repairs for the malformed-ISO family models emit:
    'Z' glued to an explicit offset ('...09:00:00.000Z+05:30', seen live —
    the composed date/time were RIGHT, only the format was junk) and
    doubled offsets ('...+05:30+04:00', the original day-one crash).
    Keep the first explicit offset; drop a redundant Z."""
    value = value.strip()
    value = re.sub(r"Z(?=[+-]\d{2}:?\d{2}$)", "", value)
    match = re.match(r"^(.*?[+-]\d{2}:\d{2})[+-]\d{2}:\d{2}$", value)
    if match:
        value = match.group(1)
    return value


def _parse_when(when_iso: str) -> datetime:
    """The extraction prompt asks the model for an offset-aware ISO datetime,
    but models sometimes drop the offset — if so, assume it meant
    KYRAAN_TIMEZONE (the same tz "now" was expressed in) rather than
    crashing or silently assuming UTC/system tz."""
    when_iso = _sanitize_iso(when_iso)
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
        # seen live: "call rohan at 7pm" came back 19:00:00.000Z, which
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


REPEAT_CHOICES = ("daily", "weekdays", "weekly", "monthly", "interval")
_MIN_INTERVAL_MINUTES = 5    # hard floor — below this is a runaway pager
CONFIRM_INTERVAL_MINUTES = 15  # under this, creation shows the owner the
                               # pings-per-day math and needs a yes:
                               # "every 5 mins, 10AM-9PM" = 133 pings a day
                               # (owner's 2026-08-26 choice: allow, gated)


def pings_per_day(interval_minutes: int, window_start: str = "",
                  window_end: str = "") -> int:
    """How many messages a day an interval series produces — the number
    the owner sees in the sub-15-minute confirm ask."""
    if window_start and window_end:
        sh, sm = (int(x) for x in window_start.split(":"))
        eh, em = (int(x) for x in window_end.split(":"))
        minutes = (eh * 60 + em) - (sh * 60 + sm)
        if minutes < 0:
            minutes += 24 * 60   # overnight window (22:00-07:00)
    else:
        minutes = 24 * 60
    return max(minutes // max(interval_minutes, 1) + 1, 1)


def advance_occurrence(when, repeat: str):
    """The next occurrence after `when` for a repeat rule. Monthly clamps
    to the shortest month (Jan 31 -> Feb 28) rather than skipping."""
    import calendar as _calendar

    if repeat == "daily":
        return when + timedelta(days=1)
    if repeat == "weekly":
        return when + timedelta(days=7)
    if repeat == "weekdays":
        step = when + timedelta(days=1)
        while step.weekday() >= 5:  # Sat/Sun
            step += timedelta(days=1)
        return step
    if repeat == "interval":
        raise ValueError("interval repeats advance via advance_for(record)")
    if repeat == "monthly":
        year = when.year + (when.month // 12)
        month = when.month % 12 + 1
        day = min(when.day, _calendar.monthrange(year, month)[1])
        return when.replace(year=year, month=month, day=day)
    raise ValueError(f"unknown repeat rule {repeat!r}")


def advance_past_now(record) -> "datetime":
    """The next FUTURE occurrence: after downtime a single advance still
    lands in the past, and rescheduling a past time fires immediately —
    a catch-up burst of stale reminders (Bugbot P1). Missed occurrences
    are skipped, not replayed; the one late send already happened."""
    next_when = advance_for(record)
    skipped = 0
    if record.repeat == "interval" and next_when <= local_now():
        # Arithmetic jump: a 5-minute series stale for a year would need
        # ~100k loop steps (audit round 2 — the cap could exit still in
        # the past AND stall the event loop); integer math does it in one.
        step = timedelta(minutes=max(record.interval_minutes, _MIN_INTERVAL_MINUTES))
        now = local_now()
        # The daily-grid shortcut below models a series that fires
        # SEVERAL TIMES inside its window — that is the only shape whose
        # slots form a within-day grid. The valid boundary is the WINDOW
        # length, not 24h: a 23h step in an 11:00-wide window overflows
        # every single time, so advance_for's real semantics collapse to
        # "daily at window_start" while the grid computed window_start +
        # k*23h (Bugbot P1 round 5). Anything stepping past the window —
        # 23-hourly, weekly, every-2-days — falls through to the bounded
        # loop below, which IS advance_for iterated: exact semantics, and
        # each iteration advances at least a day, so it is cheap.
        window_len = None
        overnight = False
        if record.window_start and record.window_end:
            _sh, _sm = (int(x) for x in record.window_start.split(":"))
            _eh, _em = (int(x) for x in record.window_end.split(":"))
            _span = (_eh * 60 + _em) - (_sh * 60 + _sm)
            overnight = _span < 0            # 22:00-07:00 wraps midnight
            window_len = timedelta(minutes=_span + (24 * 60 if overnight else 0))
        # The grid below reasons in "today's date", which an overnight
        # window straddles — those fall through to the bounded loop
        # (advance_for iterated), which handles the wrap correctly.
        if window_len is not None and not overnight and step <= window_len:
            # Windowed series re-anchor their phase at window_start on
            # EVERY rollover (advance_for's day-to-day behavior), so
            # continuous step arithmetic across skipped days drifts the
            # cadence whenever the interval doesn't divide the day
            # evenly — a 50-min 10:00-21:00 series resumed at 11:20
            # when today's true grid is 10:00/10:50/11:40 (Bugbot P2).
            # Compute today's grid slot directly instead.
            from datetime import time as _time
            sh, sm = (int(x) for x in record.window_start.split(":"))
            eh, em = (int(x) for x in record.window_end.split(":"))
            # Normalize to the LOCAL zone: the window ("10:00-21:00") is
            # wall-clock local, so date equality and the end-of-window
            # time() check are only meaningful there — a record stored
            # with a different offset kept its own zone through the
            # arithmetic and compared 17:10 against a 21:00 window.
            base = _parse_when(record.when_iso).astimezone(now.tzinfo)
            ws_today = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            # SAME-DAY catch-up keeps the record's OWN phase: a series
            # whose next slot was 10:30 stays on :30 slots today —
            # re-anchoring to window_start shifted the phase for a mere
            # minutes-long outage (Bugbot P2 round 3). The window_start
            # re-anchor is only what a real day ROLLOVER does.
            anchor = base if base.date() == now.date() else ws_today
            if now < anchor:
                next_when = anchor
            else:
                k = (now - anchor) // step + 1
                next_when = anchor + step * k
                skipped += k
            if (next_when.date() != now.date()
                    or next_when.time() > _time(eh, em)):
                # past today's last slot (including a grid step that
                # crossed midnight) -> tomorrow's window start
                next_when = ws_today + timedelta(days=1)
        elif window_len is None:
            # No window: every slot is base + k*step, so one jump lands
            # exactly on the grid.
            k = (now - next_when) // step + 1
            # The FINAL step goes through advance_for so any future rule
            # additions still snap — a bare += landed a 10:00-21:00
            # series at 02:00 after long downtime (audit round 3, P1).
            next_when = advance_for(record, from_when=next_when + step * (k - 1))
            skipped += k
    while next_when <= local_now() and skipped < 5000:
        # remaining steps are calendar-sized (window realignment, daily/
        # weekly/monthly) — a handful in practice
        next_when = advance_for(record, from_when=next_when)
        skipped += 1
    if next_when <= local_now():
        # Absolute guarantee, every rule type: one advance FROM now is
        # always in the future.
        next_when = advance_for(record, from_when=local_now())
    if skipped:
        log_event("reminder_catchup_skipped", reminder_id=record.id,
                  skipped=skipped, resumed=next_when.isoformat())
    return next_when


def advance_for(record, from_when=None) -> "datetime":
    """Next occurrence for a full record — handles interval-with-window
    rules; simple rules delegate to advance_occurrence. `from_when`
    overrides the record's own base so catch-up can step repeatedly."""
    from datetime import time as _time

    when = from_when if from_when is not None else _parse_when(record.when_iso)
    if record.repeat != "interval":
        return advance_occurrence(when, record.repeat)
    nxt = when + timedelta(minutes=max(record.interval_minutes, _MIN_INTERVAL_MINUTES))
    if record.window_start and record.window_end:
        start_h, start_m = (int(x) for x in record.window_start.split(":"))
        end_h, end_m = (int(x) for x in record.window_end.split(":"))
        start_t, end_t = _time(start_h, start_m), _time(end_h, end_m)
        if start_t <= end_t:
            if nxt.time() > end_t:
                nxt = (nxt + timedelta(days=1)).replace(hour=start_h, minute=start_m,
                                                        second=0, microsecond=0)
            elif nxt.time() < start_t:
                nxt = nxt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        elif end_t < nxt.time() < start_t:
            # OVERNIGHT window (22:00-07:00): "inside" wraps midnight, so
            # the plain comparisons rejected almost every slot and snapped
            # it to window_start — an hourly 22:00-07:00 series fired once
            # a night instead of nine times (Bugbot P2). Only a time in
            # the DAYTIME gap is outside; it moves to tonight's start.
            nxt = nxt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    return nxt


_FILLER_WORDS = frozenset(
    "remind reminder me to please a the my for about".split())


def _text_essence(text: str) -> frozenset:
    """The content words of a reminder, filler stripped. The model phrases
    the same intent differently between calls ('Call mom' vs 'Remind me to
    call mom' — seen in eval, created two pings for one intent), so
    duplicate detection must compare meaning-bearing words, not strings."""
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return frozenset(w for w in words if w not in _FILLER_WORDS)


def find_duplicate(chat_id: int, text: str, when_iso: str,
                   repeat: str = "") -> store.Reminder | None:
    """An existing pending reminder for the same intent. Found live: "ok
    then set a reminder for it" after the reminder already existed
    silently created a second identical one — two pings for one intent.
    Texts match when either's content words contain the other's (phrasing
    varies). One-shots are duplicates only at the SAME moment (call mom
    at 8 and again at 9 is legitimate); recurring reminders are a SERIES
    — same intent recurring twice is one intent regardless of the next
    occurrence's clock time (live 2026-08-26: a re-request of the hourly
    water series passed the moment check via a different when_iso and
    would have double-pinged every hour)."""
    when = _parse_when(when_iso)
    essence = _text_essence(text)
    if not essence:
        return None
    for r in store.list_pending(chat_id):
        other = _text_essence(r.text)
        if not other or not (essence <= other or other <= essence):
            continue
        if repeat and r.repeat:
            return r
        try:
            if _parse_when(r.when_iso) == when:
                return r
        except ValueError:
            continue
    return None


def create_reminder(chat_id: int, text: str, when_iso: str, repeat: str = "",
                    interval_minutes: int = 0, window_start: str = "",
                    window_end: str = "") -> store.Reminder:
    # Validate before persisting — a bad when_iso (e.g. a model producing a
    # duplicated UTC offset, seen live) must never be written to disk, or
    # it becomes a landmine that re-crashes init() on every future startup.
    _parse_when(when_iso)
    if repeat and repeat not in REPEAT_CHOICES:
        raise ValueError(f"repeat must be one of {REPEAT_CHOICES} (or empty)")
    if repeat == "interval":
        if interval_minutes < _MIN_INTERVAL_MINUTES:
            raise ValueError(f"the smallest interval is {_MIN_INTERVAL_MINUTES} minutes "
                             "— more often than that is spam, not help")
        for value in (window_start, window_end):
            if value:
                hh, mm = value.split(":")
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    raise ValueError(f"bad window time {value!r}")
    reminder = store.add(chat_id=chat_id, text=text, when_iso=when_iso, repeat=repeat)
    if repeat == "interval":
        _apply_interval_fields(reminder.id, interval_minutes, window_start, window_end)
        reminder = store.get(reminder.id)
    _schedule(reminder)
    log_event("reminder_created", reminder_id=reminder.id, chat_id=chat_id,
              when=when_iso, repeat=repeat or None)
    return reminder


def _apply_interval_fields(reminder_id: str, interval_minutes: int,
                           window_start: str, window_end: str) -> None:
    from kyraan.control_plane.filelock import locked
    with locked(store.REMINDERS_PATH):
        records = store._load_all()
        for record in records:
            if record["id"] == reminder_id:
                record["interval_minutes"] = interval_minutes
                record["window_start"] = window_start
                record["window_end"] = window_end
        store._save_all(records)


def cancel_reminder(reminder_id: str) -> bool:
    if _cancel_fn:
        _cancel_fn(reminder_id)
    return store.cancel(reminder_id)
