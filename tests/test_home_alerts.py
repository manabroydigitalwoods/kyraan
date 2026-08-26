"""Proactive home intelligence: pure decision logic + gating."""
import pytest

from kyraan.control_plane import config
from kyraan.triggers import home_alerts


@pytest.fixture(autouse=True)
def isolated_markers(monkeypatch, tmp_path):
    monkeypatch.setattr(home_alerts, "MARKERS_PATH", tmp_path / "home_alerts.json")
    base = config.load()
    monkeypatch.setattr(config, "load", lambda: {
        **base, "home_alerts": {"enabled": True, "ac_max_hours": 6, "daily_kwh_alert": 8.0}})


def test_long_ac_stretch_alerts_once_per_stretch():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=7)).isoformat()
    ac = {"state": "on", "last_changed": since}

    due = home_alerts.decide_alerts(ac, None, {}, now=now)
    assert len(due) == 1 and "7 hours straight" in due[0][2]

    # same stretch already alerted -> silent
    key, value, _ = due[0]
    assert home_alerts.decide_alerts(ac, None, {key: value}, now=now) == []

    # a NEW stretch (AC cycled off and on) alerts again when long enough
    new_since = (now - timedelta(hours=6, minutes=5)).isoformat()
    again = home_alerts.decide_alerts({"state": "on", "last_changed": new_since},
                                      None, {key: value}, now=now)
    assert len(again) == 1


def test_short_run_and_off_state_stay_silent():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fresh = {"state": "on", "last_changed": (now - timedelta(hours=2)).isoformat()}
    assert home_alerts.decide_alerts(fresh, None, {}, now=now) == []
    off = {"state": "off", "last_changed": (now - timedelta(hours=20)).isoformat()}
    assert home_alerts.decide_alerts(off, None, {}, now=now) == []


def test_daily_kwh_alert_fires_once_per_day():
    ac = {"state": "off", "last_changed": ""}
    due = home_alerts.decide_alerts(ac, {"state": "9.2"}, {})
    assert len(due) == 1 and "9.2 kWh" in due[0][2]

    key, value, _ = due[0]
    assert home_alerts.decide_alerts(ac, {"state": "10.0"}, {key: value}) == []
    assert home_alerts.decide_alerts(ac, {"state": "3.1"}, {}) == []  # under the limit


async def test_check_respects_the_proactive_gate(monkeypatch):
    from kyraan.control_plane import kernel

    monkeypatch.setattr(kernel, "can_send_proactively", lambda: False)
    sends = []

    async def send_fn(chat_id, text):
        sends.append(text)

    assert await home_alerts.check(1, send_fn) == 0
    assert sends == []


async def test_check_sends_and_marks(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from kyraan.control_plane import kernel

    monkeypatch.setattr(kernel, "can_send_proactively", lambda: True)
    since = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()

    async def fake_run_tool(call, **kwargs):
        if call.args["entity"] == "switch.ac":
            return {"state": "on", "last_changed": since}
        return {"state": "2.0", "unit": "kWh"}

    monkeypatch.setattr(kernel, "run_tool", fake_run_tool)
    sends = []

    async def send_fn(chat_id, text):
        sends.append(text)

    assert await home_alerts.check(1, send_fn) == 1
    assert "hours straight" in sends[0]
    assert await home_alerts.check(1, send_fn) == 0  # marker persists
