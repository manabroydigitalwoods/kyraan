"""Read-after-write verification + termination taxonomy (the two
adoptions from the 2026-08-31 external loop-engineering review)."""
import pytest

from kyraan.agents import loop_tools
from kyraan.control_plane import kernel


def _fake_kernel(monkeypatch, responses: dict, calls: list):
    async def run(call, **kw):
        calls.append((call.tool_name, dict(call.args)))
        out = responses[call.tool_name]
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(kernel, "run_tool", run)


async def test_switch_write_is_verified_against_observed_state(monkeypatch):
    calls = []
    _fake_kernel(monkeypatch, {"home.turn_on": {"ok": True},
                               "home.get_state": {"state": "on"}}, calls)
    out = await loop_tools.TOOLS["home.turn_on"]["run"](7, {"entity": "switch.ac"}, "")
    assert out["verified"] is True
    assert calls[-1][0] == "home.get_state"


async def test_mismatch_is_honest_not_successful(monkeypatch):
    calls = []
    _fake_kernel(monkeypatch, {"home.turn_on": {"ok": True},
                               "home.get_state": {"state": "off"}}, calls)
    out = await loop_tools.TOOLS["home.turn_on"]["run"](7, {"entity": "switch.ac"}, "")
    assert out["verified"] is False
    assert "never claim success" in out["verify_note"]


async def test_unreadable_verification_degrades_to_unchecked(monkeypatch):
    calls = []
    _fake_kernel(monkeypatch, {"home.turn_on": {"ok": True},
                               "home.get_state": kernel.ToolFailed("HA down")},
                 calls)
    out = await loop_tools.TOOLS["home.turn_on"]["run"](7, {"entity": "switch.ac"}, "")
    assert out["verified"] is None
    assert "not that it is confirmed" in out["verify_note"]


async def test_created_event_start_is_reread(monkeypatch):
    calls = []
    _fake_kernel(monkeypatch, {
        "calendar.create_event": {"id": "ev9", "title": "Lunch"},
        "calendar.get_event": {"id": "ev9",
                               "start": "2027-01-05T13:00:00+05:30"}}, calls)
    out = await loop_tools._calendar_create(
        7, {"title": "Lunch", "start": "2027-01-05T13:00:00+05:30",
            "end": "2027-01-05T14:00:00+05:30"}, "")
    assert out["verified"] is True
    # offset-equivalent times still verify (Google may answer in UTC)
    calls.clear()
    _fake_kernel(monkeypatch, {
        "calendar.create_event": {"id": "ev9", "title": "Lunch"},
        "calendar.get_event": {"id": "ev9",
                               "start": "2027-01-05T07:30:00+00:00"}}, calls)
    out = await loop_tools._calendar_create(
        7, {"title": "Lunch", "start": "2027-01-05T13:00:00+05:30",
            "end": "2027-01-05T14:00:00+05:30"}, "")
    assert out["verified"] is True


def test_contracts_name_the_verified_writes():
    from kyraan.tools import registry
    c = registry.contracts()
    assert c["calendar.create_event"]["verification"] == "read_after_write"
    assert c["home.turn_on"]["verification"] == "read_after_write"
    assert c["calendar.delete_event"]["verification"] is None


async def test_termination_is_named_on_a_reply(monkeypatch):
    from kyraan.agents import agent_loop
    agent_loop._termination.set("tier_failed:aborted_mid_loop")
    agent_loop._termination.set("replied")
    assert agent_loop.termination() == "replied"
