"""Quiet-hours gating for proactive (unsolicited) output.

User-initiated chat is never gated — only reminders/briefs/curiosity questions
that Kyraan sends on its own.
"""
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

from kyraan.control_plane import config


def humanize(value) -> str:
    """One user-facing time format for every surface: 12-hour clock, date
    only when it isn't today, year only when it isn't this year.
    "7:30 PM" / "Tue 26 Aug, 3:00 PM" / "Fri 2 Jan 2099, 5:00 PM".
    Accepts an aware/naive datetime or an ISO string (naive = KYRAAN_TIMEZONE)."""
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    dt = dt.astimezone(_tz())
    clock = dt.strftime("%I:%M %p").lstrip("0")
    today = local_now().date()
    if dt.date() == today:
        return clock
    day = f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}"
    if dt.year != today.year:
        day += f" {dt.year}"
    return f"{day}, {clock}"


def _tz() -> ZoneInfo:
    dnd_cfg = config.load()["dnd"]
    tz_name = os.environ.get(dnd_cfg["timezone_env"], "UTC")
    return ZoneInfo(tz_name)


def local_now() -> datetime:
    """The single source of truth for "now" in KYRAAN_TIMEZONE — used
    anywhere a wall-clock time needs to match what the user meant (reminder
    scheduling, DND), independent of the host machine's own system tz."""
    return datetime.now(_tz())


def in_quiet_hours(now: datetime | None = None) -> bool:
    dnd_cfg = config.load()["dnd"]
    tz = _tz()
    now = (now or datetime.now(tz)).astimezone(tz)

    start = time.fromisoformat(dnd_cfg["quiet_hours"]["start"])
    end = time.fromisoformat(dnd_cfg["quiet_hours"]["end"])
    current = now.time()

    if start <= end:
        return start <= current < end
    # window wraps midnight, e.g. 22:00 -> 07:00
    return current >= start or current < end
