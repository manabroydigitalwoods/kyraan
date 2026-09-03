"""House steward — duty #2 (2026-09-03)."""
import asyncio
from datetime import date, datetime

from kyraan.triggers import house_steward as hs


def test_filter_lines_fire_once_per_level_and_rearm_after_replacement():
    markers = {}
    assert hs.filter_lines("72", "95", markers, "d") == []
    first = hs.filter_lines("18", "95", markers, "d")
    assert len(first) == 1 and "NanoProtect" in first[0] and "soon" in first[0]
    assert hs.filter_lines("17", "95", markers, "d") == []                    # said already
    urgent = hs.filter_lines("9", "15", markers, "d")
    assert len(urgent) == 2 and "now" in urgent[0] and "wash" in urgent[1]
    assert hs.filter_lines("100", "100", markers, "d") == [] and "filter:nano" not in markers   # replaced


def test_month_line_compares_from_the_ledger():
    energy = {"2026-07-05": 2.0, "2026-07-20": 2.0, "2026-08-02": 3.0, "2026-08-15": 2.2}
    assert hs.month_line(energy, date(2026, 9, 1)) == "⚡ AC in August: 5 kWh — 30% more than July (4 kWh)."
    assert hs.month_line(energy, date(2026, 9, 2)) == ""                       # only on the 1st
    assert hs.month_line({"2026-08-02": 3.0}, date(2026, 9, 1)) == "⚡ AC in August: 3 kWh."


def test_settle_lines_only_for_things_worth_a_hand():
    assert hs.settle_lines("off", "0", {"power": "on", "mode": "sleep", "timer": "Off"}, "off") == []
    assert hs.settle_lines("on", "18", {}, "off") == []                        # idle AC: not worth it
    lines = hs.settle_lines("on", "1150", {"power": "on", "mode": "turbo", "timer": "Off"}, "on")
    assert len(lines) == 3 and "1150 W" in lines[0] and "turbo" in lines[1] and "display" in lines[2]
    assert hs.settle_lines("on", "1150", {"power": "on", "mode": "turbo", "timer": "8h"}, "off")[1:] == []


def test_fire_settle_samples_ledger_and_is_silent_when_settled(monkeypatch, tmp_path):
    monkeypatch.setattr(hs, "STATE_PATH", tmp_path / "s.json")
    readings = {"ac": "off", "ac_w": "0", "ac_today": "3.656", "nano": "72", "pre": "95", "backlight": "off"}

    async def read(key): return readings.get(key)
    monkeypatch.setattr(hs, "_read", read)
    from kyraan.tools import home_assistant
    monkeypatch.setattr(home_assistant, "purifier_state", lambda: {"power": "on", "mode": "auto", "timer": "Off"})
    monkeypatch.setattr(hs.kernel, "can_send_proactively", lambda **kw: True)
    monkeypatch.setattr(hs, "local_now", lambda: datetime(2026, 9, 3, 21, 45))
    sent = []

    async def send(chat_id, text):
        sent.append(text); return True
    assert asyncio.run(hs.fire_settle(1, send)) is False and sent == []
    assert hs._load()["energy"]["2026-09-03"] == 3.656                           # sampled anyway
    readings.update({"ac": "on", "ac_w": "1200"})
    assert asyncio.run(hs.fire_settle(1, send)) is True and "AC is on" in sent[0]
