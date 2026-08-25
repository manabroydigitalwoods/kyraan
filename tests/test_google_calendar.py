"""Google Calendar adapter tests — fixture ICS, no network. Covers the
window filter, RRULE expansion, all-day normalization, and credential/
error classification."""
import pytest

from kyraan.tools import google_calendar, registry

FIXTURE_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:one@test
SUMMARY:Dentist
DTSTART:20260826T090000Z
DTEND:20260826T100000Z
LOCATION:Clinic
END:VEVENT
BEGIN:VEVENT
UID:standup@test
SUMMARY:Standup
DTSTART:20260824T040000Z
DTEND:20260824T041500Z
RRULE:FREQ=DAILY;COUNT=10
END:VEVENT
BEGIN:VEVENT
UID:allday@test
SUMMARY:Holiday
DTSTART;VALUE=DATE:20260826
DTEND;VALUE=DATE:20260827
END:VEVENT
BEGIN:VEVENT
UID:outside@test
SUMMARY:Far future
DTSTART:20261201T090000Z
DTEND:20261201T100000Z
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def fixture_ics(monkeypatch):
    monkeypatch.setattr(google_calendar, "_fetch_ics", lambda: FIXTURE_ICS)


async def test_window_filters_and_sorts(fixture_ics):
    events = await google_calendar.call(
        "calendar.list_events",
        {"start": "2026-08-26T00:00:00+00:00", "end": "2026-08-26T23:59:59+00:00"},
    )
    titles = [e["title"] for e in events]
    assert "Dentist" in titles and "Holiday" in titles and "Standup" in titles
    assert "Far future" not in titles
    starts = [e["start"] for e in events]
    assert starts == sorted(starts)


async def test_rrule_expands_per_occurrence(fixture_ics):
    events = await google_calendar.call(
        "calendar.list_events",
        {"start": "2026-08-24T00:00:00+00:00", "end": "2026-08-28T00:00:00+00:00"},
    )
    standups = [e for e in events if e["title"] == "Standup"]
    assert len(standups) == 4  # daily rule, one per day in the 4-day window


async def test_all_day_flag_and_fields(fixture_ics):
    events = await google_calendar.call(
        "calendar.list_events",
        {"start": "2026-08-26T00:00:00+00:00", "end": "2026-08-26T23:59:59+00:00"},
    )
    holiday = next(e for e in events if e["title"] == "Holiday")
    dentist = next(e for e in events if e["title"] == "Dentist")
    assert holiday["all_day"] is True
    assert dentist["all_day"] is False
    assert dentist["location"] == "Clinic"


async def test_missing_url_is_a_clear_setup_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_CALENDAR_ICS_URL", raising=False)
    with pytest.raises(registry.ToolError, match="GOOGLE_CALENDAR_ICS_URL"):
        await google_calendar.call(
            "calendar.list_events",
            {"start": "2026-08-26T00:00:00+00:00", "end": "2026-08-26T23:59:59+00:00"},
        )


async def test_unknown_tool_name_rejected(fixture_ics):
    with pytest.raises(registry.ToolError, match="does not provide"):
        await google_calendar.call("calendar.delete_everything", {})


async def test_create_without_oauth_setup_gives_the_setup_instruction(monkeypatch):
    for var in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(registry.ToolError, match="setup_google_oauth"):
        await google_calendar.call(
            "calendar.create_event",
            {"title": "x", "start": "2026-08-26T17:00:00+05:30", "end": "2026-08-26T18:00:00+05:30"},
        )


async def test_create_event_posts_the_right_payload(monkeypatch):
    import io, json, urllib.request

    monkeypatch.setattr(google_calendar, "_access_token", lambda: "tok123")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.data)
        return io.BytesIO(json.dumps({"id": "ev1", "htmlLink": "https://cal/ev1"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = await google_calendar.call(
        "calendar.create_event",
        {"title": "Call Suman", "start": "2026-08-26T17:00:00+05:30",
         "end": "2026-08-26T18:00:00+05:30", "location": "Office"},
    )
    assert captured["auth"] == "Bearer tok123"
    assert captured["payload"] == {
        "summary": "Call Suman",
        "start": {"dateTime": "2026-08-26T17:00:00+05:30"},
        "end": {"dateTime": "2026-08-26T18:00:00+05:30"},
        "location": "Office",
    }
    assert result == {"id": "ev1", "link": "https://cal/ev1", "title": "Call Suman"}


async def test_listing_exposes_api_event_ids(fixture_ics):
    events = await google_calendar.call(
        "calendar.list_events",
        {"start": "2026-08-26T00:00:00+00:00", "end": "2026-08-26T23:59:59+00:00"},
    )
    for event in events:
        assert event["id"] and "@" not in event["id"]  # api id, not the full ICS UID
        assert "recurring" in event


async def test_delete_event_calls_the_right_endpoint(monkeypatch):
    import io
    import urllib.request

    monkeypatch.setattr(google_calendar, "_access_token", lambda: "tok123")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.headers.get("Authorization")
        return io.BytesIO(b"")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = await google_calendar.call("calendar.delete_event", {"event_id": "abc123", "title": "Test Event"})
    assert captured["url"].endswith("/events/abc123")
    assert captured["method"] == "DELETE"
    assert captured["auth"] == "Bearer tok123"
    assert result == {"id": "abc123", "deleted": True, "already_gone": False}


async def test_delete_event_treats_gone_as_done(monkeypatch):
    """A double-cancel (or an event removed in the Google UI meanwhile)
    must read as 'already gone', not an error."""
    import urllib.error
    import urllib.request

    monkeypatch.setattr(google_calendar, "_access_token", lambda: "tok123")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 410, "Gone", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = await google_calendar.call("calendar.delete_event", {"event_id": "abc123"})
    assert result == {"id": "abc123", "deleted": False, "already_gone": True}


async def test_recurring_flag_from_original_vevents_not_expansion(monkeypatch):
    """The expansion library stamps RECURRENCE-ID on EVERY occurrence it
    emits — one-off events included — so recurrence must be read from the
    original VEVENTs (live: all four events in a delete ask carried a
    false whole-series warning)."""
    ics = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"""
    ics += b"""BEGIN:VEVENT\r\nUID:oneoff@google.com\r\nDTSTART:20990102T100000Z\r\nDTEND:20990102T110000Z\r\nSUMMARY:One-off\r\nEND:VEVENT\r\n"""
    ics += b"""BEGIN:VEVENT\r\nUID:daily@google.com\r\nDTSTART:20990102T090000Z\r\nDTEND:20990102T093000Z\r\nRRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:Daily standup\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
    monkeypatch.setattr(google_calendar, "_fetch_ics", lambda: ics)

    events = await google_calendar.call(
        "calendar.list_events",
        {"start": "2099-01-02T00:00:00+00:00", "end": "2099-01-02T23:59:59+00:00"},
    )
    flags = {e["title"]: e["recurring"] for e in events}
    assert flags == {"One-off": False, "Daily standup": True}
