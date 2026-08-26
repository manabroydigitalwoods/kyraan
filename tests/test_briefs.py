"""Morning brief — deterministic compose, proactive gating, config parse.
Calendar tool and reminder store are faked; no network, no model."""
import os

import pytest

os.environ.setdefault("KYRAAN_TIMEZONE", "UTC")

from kyraan.control_plane import config, kernel, kill_switch
from kyraan.control_plane.dnd import local_now
from kyraan.triggers import briefs, store


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "REMINDERS_PATH", tmp_path / "reminders.json")
    yield


@pytest.fixture
def fake_calendar(monkeypatch):
    events = []

    async def fake_run_tool(call, **kwargs):
        if call.tool_name == "home.get_state":
            raise kernel.ToolFailed("HA not configured in this test")
        assert call.tool_name == "calendar.list_events"
        return events

    monkeypatch.setattr(kernel, "run_tool", fake_run_tool)
    return events


async def test_compose_shows_events_and_todays_reminders(isolated_store, fake_calendar):
    fake_calendar.append(
        {"title": "Standup", "start": f"{local_now().date()}T09:30:00+00:00",
         "end": f"{local_now().date()}T09:45:00+00:00", "all_day": False, "location": "Meet"}
    )
    today = local_now().replace(hour=23, minute=0, second=0, microsecond=0)
    store.add(chat_id=1, text="call Rohan", when_iso=today.isoformat())
    store.add(chat_id=1, text="far future", when_iso="2099-01-01T10:00:00+00:00")  # not today
    store.add(chat_id=2, text="someone else's", when_iso=today.isoformat())  # other chat

    text = await briefs.compose(1)

    assert "Morning brief" in text
    assert "9:30 AM — Standup (Meet)" in text
    assert "11:00 PM — call Rohan" in text
    assert "far future" not in text and "someone else's" not in text


async def test_compose_handles_empty_day(isolated_store, fake_calendar):
    text = await briefs.compose(1)
    assert "Nothing on the calendar today." in text
    assert "No reminders today." in text


async def test_compose_survives_calendar_failure(isolated_store, monkeypatch):
    async def broken_run_tool(call, **kwargs):
        raise kernel.ToolFailed("calendar.list_events failed: feed unreachable")

    monkeypatch.setattr(kernel, "run_tool", broken_run_tool)
    text = await briefs.compose(1)
    assert "Couldn't check the calendar" in text
    assert "No reminders today." in text  # the rest of the brief still composed


async def test_fire_sends_through_the_proactive_gate(isolated_store, fake_calendar, monkeypatch):
    monkeypatch.setattr(kernel, "can_send_proactively", lambda: True)  # wall-clock independence
    sends = []

    async def send_fn(chat_id, text):
        sends.append((chat_id, text))

    assert await briefs.fire(1, send_fn) is True
    assert sends and sends[0][0] == 1 and "Morning brief" in sends[0][1]


async def test_fire_is_blocked_by_the_kill_switch(isolated_store, fake_calendar):
    sends = []

    async def send_fn(chat_id, text):
        sends.append(1)

    kill_switch.engage("test")
    try:
        assert await briefs.fire(1, send_fn) is False
    finally:
        kill_switch.disengage()
    assert sends == []


def test_brief_time_parses_config(monkeypatch):
    base = config.load()
    monkeypatch.setattr(config, "load", lambda: {**base, "briefs": {"morning": {"enabled": True, "time": "07:30"}}})
    at = briefs.brief_time()
    assert (at.hour, at.minute) == (7, 30)

    monkeypatch.setattr(config, "load", lambda: {**base, "briefs": {"morning": {"enabled": False}}})
    assert briefs.brief_time() is None

    monkeypatch.setattr(config, "load", lambda: {**base, "briefs": {}})
    assert briefs.brief_time() is None  # unconfigured = disabled, never a crash


async def test_brief_notes_the_ac_when_it_is_on(isolated_store, monkeypatch):
    async def fake_run_tool(call, **kwargs):
        if call.tool_name == "calendar.list_events":
            return []
        if call.args["entity"] == "switch.ac":
            return {"entity": "switch.ac", "state": "on", "unit": None, "name": "AC"}
        return {"entity": call.args["entity"], "state": "359.5", "unit": "W", "name": "AC power"}

    monkeypatch.setattr(kernel, "run_tool", fake_run_tool)
    text = await briefs.compose(1)
    assert "⚡ The AC is ON — drawing 359.5 W." in text


async def test_evening_brief_covers_tomorrow_and_the_energy_story(isolated_store, monkeypatch):
    from kyraan.control_plane.dnd import local_now
    from datetime import timedelta

    tomorrow = (local_now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    store.add(chat_id=1, text="dentist", when_iso=tomorrow.isoformat())

    async def fake_run_tool(call, **kwargs):
        if call.tool_name == "calendar.list_events":
            return [{"title": "Standup", "start": tomorrow.isoformat(),
                     "end": tomorrow.isoformat(), "all_day": False, "location": None}]
        if call.args["entity"] == "sensor.ac_today_s_consumption":
            return {"state": "3.4", "unit": "kWh"}
        return {"state": "off"}

    monkeypatch.setattr(kernel, "run_tool", fake_run_tool)
    text = await briefs.compose_evening(1)
    assert "Evening brief" in text
    assert "Tomorrow:" in text and "Standup" in text
    assert "dentist" in text
    assert "AC used 3.4 kWh today." in text
    assert "still ON" not in text


def test_evening_brief_time_from_config(monkeypatch):
    base = config.load()
    monkeypatch.setattr(config, "load", lambda: {
        **base, "briefs": {"evening": {"enabled": True, "time": "21:30"}}})
    at = briefs.brief_time("evening")
    assert (at.hour, at.minute) == (21, 30)
    assert briefs.brief_time("morning") is None  # not configured here
