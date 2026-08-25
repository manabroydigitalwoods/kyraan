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
