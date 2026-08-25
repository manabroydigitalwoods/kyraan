"""Google Calendar adapter.

Reads: the calendar's secret ICS address (zero OAuth) — Google Calendar →
Settings → [calendar] → "Integrate calendar" → "Secret address in iCal
format", pasted into .env as GOOGLE_CALENDAR_ICS_URL. The URL itself is a
credential — treat it like a password.

Writes: calendar.create_event via the Calendar API with OAuth — Google
retired non-OAuth write paths. One-time setup: scripts/setup_google_oauth.py
(needs GOOGLE_OAUTH_CLIENT_ID/SECRET from the user's GCP console, stores
GOOGLE_OAUTH_REFRESH_TOKEN). At runtime the refresh token is exchanged for
a short-lived access token with a plain stdlib POST — no Google SDK in the
service. The tool is confirm-gated by the registry's hard rule; the user
approves every single event creation.

Adapter contract (docs/design/tool_registry.md): `async def call(tool_name, args)`.
"""
import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time

import icalendar
import recurring_ical_events

from kyraan.control_plane.dnd import local_now
from kyraan.tools.registry import ToolError, TransientToolError

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


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
    # Recurrence must be read from the ORIGINAL VEVENTs: the expansion
    # library strips RRULE and stamps RECURRENCE-ID onto EVERY occurrence
    # it emits — one-off events included — so flagging from the expanded
    # occurrence marked everything "recurring" (live 2026-08-26: all four
    # events in a delete-confirm ask carried a false whole-series warning).
    recurring_uids = {
        str(v.get("UID")) for v in calendar.walk("VEVENT")
        if v.get("RRULE") or v.get("RDATE")
    }
    # recurring_ical_events expands RRULEs — a weekly standup appears once
    # per occurrence in the window, not once per VEVENT definition.
    occurrences = recurring_ical_events.of(calendar).between(window_start, window_end)

    events = []
    for occ in occurrences:
        dtstart = _as_local_datetime(occ["DTSTART"].dt)
        dtend = _as_local_datetime(occ["DTEND"].dt) if "DTEND" in occ else dtstart
        # Google's ICS UIDs are "<api event id>@google.com" — the id part
        # is what the Calendar API's delete endpoint needs. Recurring
        # occurrences share their series' UID: deleting by it removes the
        # whole series, which the cancel flow must say out loud.
        uid = str(occ.get("UID", ""))
        events.append({
            "title": str(occ.get("SUMMARY", "(no title)")),
            "start": dtstart.isoformat(),
            "end": dtend.isoformat(),
            "all_day": not isinstance(occ["DTSTART"].dt, datetime),
            "location": str(occ["LOCATION"]) if occ.get("LOCATION") else None,
            "id": uid.split("@")[0] if uid else None,
            "recurring": uid in recurring_uids,
        })
    events.sort(key=lambda e: e["start"])
    return events


from kyraan.tools.google_auth import access_token as _access_token  # shared across Google adapters


def _create_event(args: dict) -> dict:
    payload = {
        "summary": args["title"],
        "start": {"dateTime": args["start"]},
        "end": {"dateTime": args["end"]},
    }
    if args.get("location"):
        payload["location"] = args["location"]
    request = urllib.request.Request(
        _EVENTS_URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as resp:
            created = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            raise TransientToolError(f"Google Calendar returned {exc.code}") from exc
        raise ToolError(f"Google Calendar rejected the event ({exc.code}): {exc.read().decode()[:200]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Google Calendar: {exc}") from exc
    return {"id": created.get("id"), "link": created.get("htmlLink"), "title": args["title"]}


def _delete_event(args: dict) -> dict:
    event_id = args["event_id"]
    request = urllib.request.Request(
        f"{_EVENTS_URL}/{urllib.parse.quote(event_id)}",
        headers={"Authorization": f"Bearer {_access_token()}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            # Already gone — the outcome the user wanted; say so honestly
            # rather than erroring on a double-cancel.
            return {"id": event_id, "deleted": False, "already_gone": True}
        if exc.code >= 500:
            raise TransientToolError(f"Google Calendar returned {exc.code}") from exc
        raise ToolError(f"Google Calendar refused the delete ({exc.code}): {exc.read().decode()[:200]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Google Calendar: {exc}") from exc
    return {"id": event_id, "deleted": True, "already_gone": False}


async def call(tool_name: str, args: dict) -> object:
    # urllib + ICS parsing are blocking — keep the event loop free.
    if tool_name == "calendar.list_events":
        return await asyncio.to_thread(_list_events, args["start"], args["end"])
    if tool_name == "calendar.create_event":
        return await asyncio.to_thread(_create_event, args)
    if tool_name == "calendar.delete_event":
        return await asyncio.to_thread(_delete_event, args)
    raise ToolError(f"google_calendar adapter does not provide {tool_name!r}")
