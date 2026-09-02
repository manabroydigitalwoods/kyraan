"""Alexa announcements (governance 2026-09-02): allowlisted speakers,
the absolute quiet-hours refusal, the exemption ceremony, and spoken
reminders staying best-effort."""
import pytest

from kyraan.agents import loop_tools
from kyraan.control_plane import kernel
from kyraan.tools import home_assistant as ha


def test_announce_targets_are_an_allowlist(monkeypatch):
    monkeypatch.setattr(ha, "_announce_targets",
                        lambda: ["manab_s_echo_dot", "everywhere"])
    calls = []
    monkeypatch.setattr(ha, "_api", lambda path, payload: calls.append((path, payload)))
    out = ha._announce("Dinner is ready")
    assert out["on"] == "manab_s_echo_dot"          # first = default
    assert calls[0][0] == "/api/services/notify/alexa_media_manab_s_echo_dot"
    assert calls[0][1]["data"] == {"type": "announce"}
    out = ha._announce("Hello all", target="everywhere")
    assert out["on"] == "everywhere"
    with pytest.raises(ha.ToolError, match="unknown speaker"):
        ha._announce("x", target="kitchen")


def test_no_targets_is_an_honest_config_pointer(monkeypatch):
    monkeypatch.setattr(ha, "_announce_targets", lambda: [])
    with pytest.raises(ha.ToolError, match="announce_targets"):
        ha._announce("hello")


async def test_quiet_hours_refusal_is_absolute(monkeypatch):
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: False)
    with pytest.raises(kernel.ToolFailed, match="quiet hours"):
        await loop_tools._home_announce(7, {"message": "boo"}, "")


async def test_announce_is_auto_and_dnd_gated(monkeypatch):
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: True)
    ran = []

    async def fake_run(call, **kw):
        ran.append(call.args)
        return {"announced": True, "on": "manab_s_echo_dot",
                "message": call.args["message"]}
    monkeypatch.setattr(kernel, "run_tool", fake_run)
    out = await loop_tools._home_announce(7, {"message": "Dinner!"}, "")
    assert out["announced"] and ran[0]["message"] == "Dinner!"


def test_announce_is_in_the_exemption_with_ceremony():
    from kyraan.tools import registry
    assert "home.announce" in registry.MEDIA_AUTO_EXEMPT
    spec = registry.get("home.announce")
    assert spec.permission == "auto" and spec.side_effects == "notify"


async def test_spoken_reminder_failure_never_fails_the_reminder(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.channels import telegram_bot
    from kyraan.triggers import scheduler as _sched
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    captured = {}
    monkeypatch.setattr(_sched, "init",
                        lambda schedule_fn, cancel_fn, send_fn:
                        captured.update(send=send_fn))
    monkeypatch.setattr(telegram_bot.orchestrator, "record_proactive",
                        lambda cid, text: None)
    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    spoken = []
    monkeypatch.setattr(ha, "_announce_targets", lambda: ["echo"])

    def boom(msg, target=""):
        spoken.append(msg)
        raise RuntimeError("alexa down")
    monkeypatch.setattr(ha, "_announce", boom)

    class FakeJQ:
        def run_once(self, *a, **k): pass
        def get_jobs_by_name(self, n): return []

    telegram_bot._wire_scheduler(FakeJQ(), FakeBot())
    assert await captured["send"](1, "Reminder: Drink water") is True
    assert sent and spoken == ["Reminder: Drink water"]  # tried, failed, contained


def test_fan_domain_is_switchable_like_switch(monkeypatch):
    monkeypatch.setattr(ha, "_allowlists",
                        lambda: ([], ["fan.air_purifier"]))
    calls = []
    monkeypatch.setattr(ha, "_api", lambda path, payload: calls.append(path))
    monkeypatch.setattr(ha, "_get_state",
                        lambda e: {"entity": e, "state": "on"})
    out = ha._switch("fan.air_purifier", True)
    assert calls == ["/api/services/fan/turn_on"]
    assert out["converged"] is True
    with pytest.raises(ha.ToolError, match="only switch/fan"):
        monkeypatch.setattr(ha, "_allowlists", lambda: ([], ["light.x"]))
        ha._switch("light.x", True)


def test_roster_names_switchable_entities(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel as _k
    monkeypatch.setattr(_k.config, "load", lambda: {"tool_servers": {
        "home_assistant": {"read_entities": ["sensor.a"],
                           "write_entities": ["fan.air_purifier"]}}})
    roster = loop_tools._home_entity_roster()
    assert "Switchable entities" in roster and "fan.air_purifier" in roster


def test_write_wall_refusal_lists_the_allowlist(monkeypatch):
    monkeypatch.setattr(ha, "_allowlists",
                        lambda: ([], ["fan.air_purifier", "switch.ac"]))
    with pytest.raises(ha.ToolError, match="EXACTLY: fan.air_purifier, switch.ac"):
        ha._switch("switch.air_purifier", True)


def test_media_player_domain_converges_by_state_family(monkeypatch):
    monkeypatch.setattr(ha, "_allowlists",
                        lambda: ([], ["media_player.manab_s_firetvstick"]))
    calls = []
    monkeypatch.setattr(ha, "_api", lambda path, payload: calls.append(path))
    monkeypatch.setattr(ha, "_get_state",
                        lambda e: {"entity": e, "state": "idle"})
    out = ha._switch("media_player.manab_s_firetvstick", True)
    assert calls == ["/api/services/media_player/turn_on"]
    assert out["converged"] is True      # idle IS on for a media player
    monkeypatch.setattr(ha, "_get_state",
                        lambda e: {"entity": e, "state": "standby"})
    out = ha._switch("media_player.manab_s_firetvstick", False)
    assert out["converged"] is True      # standby IS off
