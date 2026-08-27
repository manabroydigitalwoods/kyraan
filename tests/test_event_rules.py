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
    assert await event_rules.tick() == 0          # cooldown holds
    assert len(ticking) == 1


async def test_dnd_hold_does_not_burn_the_rule(ticking, monkeypatch):
    _rule()
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: False)
    assert await event_rules.tick() == 0
    assert ticking == []
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: True)
    assert await event_rules.tick() == 1          # fires after quiet hours


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