"""Air purifier modes/timer/index (owner 2026-09-03)."""
import asyncio

import pytest

from kyraan.tools import home_assistant as ha
from kyraan.tools.registry import ToolError


def _fake_device(monkeypatch, state):
    """A device whose HA state follows the service calls it receives."""
    calls = []
    def get_state(entity):
        if entity == ha.PURIFIER_FAN:
            return {"state": state["power"], "attributes": {"preset_mode": state["mode"],
                    "preset_modes": ["auto", "turbo", "medium", "sleep"]}}
        if entity == ha.PURIFIER_TIMER:
            return {"state": state["timer"], "attributes": {"options": ["Off", "1h", "2h", "12h"]}}
        if entity == ha.PURIFIER_INDEX:
            return {"state": state["index"], "attributes": {"options": ["indoor_allergen_index", "pm25", "gas_level"]}}
        raise AssertionError(entity)
    def api(path, payload=None):
        calls.append((path, payload))
        if path.endswith("fan/set_preset_mode"):
            state["mode"] = payload["preset_mode"]
        elif path.endswith("fan/turn_on"):
            state["power"] = "on"
        elif path.endswith("select/select_option"):
            state["timer" if payload["entity_id"] == ha.PURIFIER_TIMER else "index"] = payload["option"]
        return {}
    monkeypatch.setattr(ha, "_raw", get_state)
    monkeypatch.setattr(ha, "_api", api)
    monkeypatch.setattr(ha, "_allowlists", lambda: ([], [ha.PURIFIER_FAN]))
    monkeypatch.setattr(ha.time, "sleep", lambda s: None)
    return calls


def test_purifier_sets_any_subset_and_reads_back(monkeypatch):
    state = {"power": "on", "mode": "turbo", "timer": "Off", "index": "pm25"}
    calls = _fake_device(monkeypatch, state)
    out = ha._purifier(mode="sleep", timer="2 h")
    assert out["converged"] and out["mode"] == "sleep" and out["timer"] == "2h" and out["index"] == "pm25"
    assert [c[0].split("/")[-1] for c in calls] == ["set_preset_mode", "select_option"]
    # index only, with the human spelling
    out = ha._purifier(index="PM2.5")
    assert out["requested"] == {"index": "pm25"} and out["index"] == "pm25"
    # "off"/"cancel" map onto the device's own "Off"
    assert ha._purifier(timer="cancel")["timer"] == "Off"


def test_purifier_validates_against_what_the_device_offers(monkeypatch):
    _fake_device(monkeypatch, {"power": "on", "mode": "auto", "timer": "Off", "index": "pm25"})
    with pytest.raises(ToolError, match="mode must be one of"):
        ha._purifier(mode="hurricane")
    with pytest.raises(ToolError, match="timer must be one of"):
        ha._purifier(timer="30 minutes")
    with pytest.raises(ToolError, match="say what to set"):
        ha._purifier()


def test_purifier_turns_on_first_when_asleep(monkeypatch):
    state = {"power": "off", "mode": "auto", "timer": "Off", "index": "pm25"}
    calls = _fake_device(monkeypatch, state)
    out = ha._purifier(mode="medium")
    assert [c[0].split("/")[-1] for c in calls[:2]] == ["turn_on", "set_preset_mode"]
    assert out["power"] == "on" and out["mode"] == "medium"


def test_loop_executor_no_op_and_receipts(monkeypatch):
    from kyraan.agents import loop_tools
    monkeypatch.setattr(ha, "purifier_state", lambda: {"mode": "sleep", "timer": "Off", "index": "pm25"})
    out = asyncio.run(loop_tools.TOOLS["home.purifier"]["run"](1, {"mode": "sleep"}, ""))
    assert out["changed"] is False
    assert loop_tools._describe_call("home.purifier", {"mode": "turbo", "timer": "2h"}, "", 1) == \
        "About to set the air purifier: mode → turbo, timer → 2h"
    assert loop_tools._confirmed_reply("home.purifier", {"mode": "turbo"},
                                       {"mode": "turbo", "timer": "Off", "index": "pm25",
                                        "requested": {"mode": "turbo"}, "converged": True}) == "Done — air purifier: mode turbo."
    undo = loop_tools.UNDO_MAP["home.purifier"]({"mode": "turbo"}, {"changed": True},
                                                {"mode": "sleep", "timer": "Off", "index": "pm25"})
    assert undo == ("home.purifier", {"mode": "sleep", "timer": "Off", "index": "pm25"})
