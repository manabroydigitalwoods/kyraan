"""Google Calendar adapter — read-only, via the calendar's secret ICS
address. Zero OAuth: Google Calendar → Settings → [calendar] →
"Integrate calendar" → "Secret address in iCal format", pasted into .env
as GOOGLE_CALENDAR_ICS_URL. The URL itself is the credential — treat it
like a password (anyone holding it can read the calendar).

Writes (create/update/delete) are NOT this adapter's job: Google requires
OAuth for those, and per the rollout plan write tools wait for the Phase 1
soak anyway. When they arrive they'll be a separate confirm-gated tool.

Adapter contract (docs/design/tool_registry.md): `async def call(tool_name, args)`.
"""
import asyncio
import os
import urllib.error
import urllib.request
from datetime import date, datetime, time

import icalendar
import recurring_ical_events

from kyraan.control_plane.dnd import local_now
from kyraan.tools.registry import ToolError, TransientToolError


def _fetch_ics() -> bytes:
    url = os.environ.get("GOOGLE_CALENDAR_ICS_URL", "").strip()
    if not url:
        raise ToolError(
            "GOOGLE_CALENDAR_ICS_URL is not set — in Google Calendar: Settings → your calendar → "
            "'Integrate calendar' → copy 'Secret address in iCal format' into .env"
        )
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            raise TransientToolError(f"Google Calendar returned {exc.code}") from exc
        raise ToolError(
            f"Google Calendar returned {exc.code} — the secret ICS URL is likely wrong or was reset"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Google Calendar: {exc}") from exc


def _as_local_datetime(value) -> datetime:
    """ICS DTSTART/DTEND can be a date (all-day) or datetime; normalize to
    an aware datetime in the user's timezone."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=local_now().tzinfo)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=local_now().tzinfo)
    raise ToolError(f"unexpected ICS time value: {value!r}")


def _list_events(start_iso: str, end_iso: str) -> list[dict]:
    window_start = datetime.fromisoformat(start_iso)
    window_end = datetime.fromisoformat(end_iso)
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=local_now().tzinfo)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=local_now().tzinfo)

    calendar = icalendar.Calendar.from_ical(_fetch_ics())
    # recurring_ical_events expands RRULEs — a weekly standup appears once
    # per occurrence in the window, not once per VEVENT definition.
    occurrences = recurring_ical_events.of(calendar).between(window_start, window_end)

    events = []
    for occ in occurrences:
        dtstart = _as_local_datetime(occ["DTSTART"].dt)
        dtend = _as_local_datetime(occ["DTEND"].dt) if "DTEND" in occ else dtstart
        events.append({
            "title": str(occ.get("SUMMARY", "(no title)")),
            "start": dtstart.isoformat(),
            "end": dtend.isoformat(),
            "all_day": not isinstance(occ["DTSTART"].dt, datetime),
            "location": str(occ["LOCATION"]) if occ.get("LOCATION") else None,
        })
    events.sort(key=lambda e: e["start"])
    return events


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "calendar.list_events":
        # urllib + ICS parsing are blocking — keep the event loop free.
        return await asyncio.to_thread(_list_events, args["start"], args["end"])
    raise ToolError(f"google_calendar adapter does not provide {tool_name!r}")
