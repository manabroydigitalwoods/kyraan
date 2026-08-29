"""Event-triggered watch rules (owner, 2026-08-27): creation
validation, the deterministic condition math, cooldown/DND semantics,
and the notify-only tick."""
from datetime import datetime, timedelta, timezone

import pytest

from kyraan.control_plane import kernel
from kyraan.triggers import event_rules


@pytest.fixture(autouse=True)
def _entities(monkeypatch):
    monkeypatch.setattr(event_rules, "_known_entities",
                        lambda: {"switch.ac", "sensor.bedroom_temp"})


def _rule(**over):
    base = dict(chat_id=7, description="AC on too long", entity="switch.ac",
                op="is", value="on", for_minutes=180)
    base.update(over)
    return event_rules.create(**base)


# --- creation -------------------------------------------------------------

def test_create_validates(monkeypatch):
    with pytest.raises(ValueError, match="op must be"):
        _rule(op="equals")
    with pytest.raises(ValueError, match="allowlist"):
        _rule(entity="switch.unknown")
    with pytest.raises(ValueError):
        _rule(op="above", value="hot")          # numeric required
    rule = _rule()
    assert rule.cooldown_minutes >= 15
    assert [r.id for r in event_rules.list_active(7)] == [rule.id]


def test_cancel_by_prefix_and_ambiguity():
    a = _rule(description="rule one")
    _rule(description="rule two")
    gone = event_rules.cancel(7, a.id)
    assert gone.id == a.id
    assert len(event_rules.list_active(7)) == 1
    with pytest.raises(ValueError, match="no active watch rule"):
        event_rules.cancel(7, "zzzz")


# --- the condition math ---------------------------------------------------

def test_is_with_duration_uses_last_changed():
    rule = _rule()
    now = datetime.now(timezone.utc)
    held_4h = {"state": "on",
               "last_changed": (now - timedelta(hours=4)).isoformat()}
    held_1h = {"state": "on",
               "last_changed": (now - timedelta(hours=1)).isoformat()}
    assert event_rules.condition_met(rule, held_4h, now) is True
    assert event_rules.condition_met(rule, held_1h, now) is False
    assert event_rules.condition_met(rule, {"state": "off"}, now) is False
    # a duration that can't be PROVEN never fires
    assert event_rules.condition_met(rule, {"state": "on"}, now) is False


def test_numeric_thresholds():
    hot = _rule(entity="sensor.bedroom_temp", op="above", value="30",
                for_minutes=0, description="too hot")
    assert event_rules.condition_met(hot, {"state": "31.5"}) is True
    assert event_rules.condition_met(hot, {"state": "29.9"}) is False
    assert event_rules.condition_met(hot, {"state": "unavailable"}) is False


# --- the tick -------------------------------------------------------------

@pytest.fixture
def ticking(monkeypatch):
    sent = []

    async def send(chat_id, text):
        sent.append((chat_id, text))

    event_rules.init(send)
    now = datetime.now(timezone.utc)

    async def fake_state(call, **kw):
        return {"state": "on",
                "last_changed": (now - timedelta(hours=4)).isoformat()}

    monkeypatch.setattr(kernel, "run_tool", fake_state)
    monkeypatch.setattr(kernel, "can_send_proactively",
                        lambda **kw: True)
    return sent


async def test_tick_fires_once_then_cooldown(ticking):
    _rule()
    assert await event_rules.tick() == 1
    assert "AC on too long" in ticking[0][1]
    assert "on" in ticking[0][1]  # the live reading rides every alert
    assert await event_rules.tick() == 0          # cooldown holds
    assert len(ticking) == 1


async def test_dnd_hold_does_not_burn_the_rule(ticking, monkeypatch):
    _rule()
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: False)
    assert await event_rules.tick() == 0
    assert ticking == []
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: True)
    assert await event_rules.tick() == 1          # fires after quiet hours


async def test_custom_message_carries_the_reading(ticking):
    # The owner got "above 27°C" with the actual 27.7 nowhere in sight.
    _rule(message="Bedroom is hot")
    await event_rules.tick()
    assert ticking[0][1] == "Bedroom is hot (now: on)"


async def test_tick_send_override_beats_init(ticking):
    # The bot passes a job-context-bound send per tick; the init() fn is
    # only the fallback. (The captured-send wiring NameError'd live.)
    _rule()
    got = []

    async def override(chat_id, text):
        got.append((chat_id, text))

    assert await event_rules.tick(send=override) == 1
    assert got and ticking == []


async def test_read_failure_is_contained(ticking, monkeypatch):
    _rule()

    async def boom(call, **kw):
        raise RuntimeError("HA down")

    monkeypatch.setattr(kernel, "run_tool", boom)
    assert await event_rules.tick() == 0          # logged, never raised


# --- loop surface ---------------------------------------------------------

async def test_executor_is_confirm_gated():
    from kyraan.agents import loop_tools
    with pytest.raises(kernel.ConfirmationRequired):
        await loop_tools._rules_create(
            7, {"description": "watch", "entity": "switch.ac",
                "op": "is", "value": "on", "for_minutes": 180}, "watch the ac")


def test_undo_entry_cancels_the_new_rule():
    from kyraan.agents.loop_tools import UNDO_MAP
    assert UNDO_MAP["rules.create"](
        {}, {"created": True, "id": "ab12cd34"}, None
    ) == ("rules.cancel", {"rule_id": "ab12cd34"})

def test_cancel_then_reactivate_round_trips():
    rule = _rule()
    event_rules.cancel(7, rule.id)
    assert event_rules.list_active(7) == []
    back = event_rules.reactivate(7, rule.id)
    assert back.id == rule.id and back.active
    assert [r.id for r in event_rules.list_active(7)] == [rule.id]
    with pytest.raises(ValueError):
        event_rules.reactivate(7, rule.id)  # already active


async def test_failed_delivery_does_not_burn_the_cooldown(ticking, monkeypatch):
    """Bugbot round-2 P1: an undelivered alert entered cooldown and was
    suppressed unheard for 2 hours. A False send leaves the rule
    unfired — the next tick retries, like the DND hold."""
    _rule()

    async def failing_send(chat_id, text):
        return False

    assert await event_rules.tick(send=failing_send) == 0
    assert await event_rules.tick() == 1   # init() send works -> fires now


async def test_hovering_condition_alerts_once_per_crossing(ticking, monkeypatch):
    """Live 2026-08-29: a bedroom hovering at 27.2 against a 27
    threshold nagged every cooldown expiry, all night. Edge-triggered:
    one alert per crossing; re-alerts only after dropping below and
    crossing again. DND holds still retry until delivered."""
    rule = _rule(entity="sensor.bedroom_temp", op="above", value="27",
                 for_minutes=0, description="too hot",
                 cooldown_minutes=15)
    reading = {"state": "27.2"}

    async def fake_state(call, **kw):
        return dict(reading)

    monkeypatch.setattr(kernel, "run_tool", fake_state)
    assert await event_rules.tick() == 1          # crossing: fires
    # cooldown expires, condition still true -> NO nag
    event_rules._mark_fired(rule.id)  # refresh stamp then age it away
    import json as _json
    records = _json.loads(event_rules.RULES_PATH.read_text())
    for r in records:
        r["last_fired_iso"] = "2020-01-01T00:00:00+00:00"
    event_rules.RULES_PATH.write_text(_json.dumps(records))
    assert await event_rules.tick() == 0          # still true: silent
    reading["state"] = "26.5"
    assert await event_rules.tick() == 0          # dropped below: re-armed
    reading["state"] = "27.4"
    assert await event_rules.tick() == 1          # new crossing: fires again


async def test_dnd_hold_still_retries_under_edge_trigger(ticking, monkeypatch):
    _rule()
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: False)
    assert await event_rules.tick() == 0          # held, last_met NOT set
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: True)
    assert await event_rules.tick() == 1          # still lands after DND
