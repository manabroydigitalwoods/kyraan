"""Soak-day verdict for the P3.2 backend flags (memory + promises):
counts the events that would dirty a soak day, per local day, from
events.jsonl + the day-wise archives.

    .venv/bin/python scripts/soak_check.py

Clean day = zero of: memory_backend_fallback, promises_backend_fallback,
promise_sync_deferred, fact_sync_deferred, episode_suppress_deferred.
Cutover rule (workplan P3.2c/P3.2d): flip a default after >=3
consecutive clean days on its flag + a green eval.
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.control_plane.dnd import local_now  # noqa: E402

_DIRTY = ("memory_backend_fallback", "promises_backend_fallback",
          "promise_sync_deferred", "fact_sync_deferred",
          "episode_suppress_deferred")
_SOAK_START = "2026-08-27"  # both flags set this day


def _event_files() -> list:
    files = [REPO / "logs" / "events.jsonl"]
    archive = REPO / "logs" / "archive"
    if archive.exists():
        # rotated files are archive/<day>/events-<stamp>-<uuid>.jsonl —
        # the bare "events.jsonl" glob matched NOTHING, so every
        # historical sync failure was invisible (Bugbot P1).
        files += sorted(archive.glob("*/events-*.jsonl"))
    # pre-archive layout: rotated files beside the live log
    files += sorted((REPO / "logs").glob("events-*.jsonl"))
    return [f for f in files if f.exists()]


def main() -> int:
    tz = local_now().tzinfo
    per_day: dict = defaultdict(Counter)
    for path in _event_files():
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = event.get("kind")  # the log's key is `kind` — filtering
            if name not in _DIRTY:    # on `event` made every day "clean"
                continue
            try:
                day = datetime.fromisoformat(event["ts"]).astimezone(tz).date().isoformat()
            except (KeyError, ValueError):
                continue
            if day >= _SOAK_START:
                per_day[day][name] += 1
    today = local_now().date().isoformat()
    start = datetime.fromisoformat(_SOAK_START).date()
    days = []
    cursor = start
    while cursor.isoformat() <= today:
        days.append(cursor.isoformat())
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
    clean_streak = 0
    for day in days:
        dirty = per_day.get(day)
        if dirty:
            detail = ", ".join(f"{k}×{v}" for k, v in sorted(dirty.items()))
            print(f"❌ {day}: {detail}")
            clean_streak = 0
        else:
            print(f"✅ {day}: clean")
            clean_streak += 1
    print(f"\nclean streak: {clean_streak} day(s) — cutover needs >=3 + green eval")
    return 0


if __name__ == "__main__":
    sys.exit(main())
