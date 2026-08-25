"""Quiet-hours gating for proactive (unsolicited) output.

User-initiated chat is never gated — only reminders/briefs/curiosity questions
that Kyraan sends on its own.
"""
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

from kyraan.control_plane import config


def _tz() -> ZoneInfo:
    dnd_cfg = config.load()["dnd"]
    tz_name = os.environ.get(dnd_cfg["timezone_env"], "UTC")
    return ZoneInfo(tz_name)


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
