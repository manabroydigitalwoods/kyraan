"""Regression pins for the 2026-08-27 repository-wide audit findings."""
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from kyraan.control_plane.dnd import local_now
from kyraan.triggers import agent_tasks, scheduler


# --- P1: recurring reminders skip missed occurrences after downtime -------

def test_reminder_catchup_skips_to_the_future():
    record = scheduler.store.Reminder if hasattr(scheduler.store, "Reminder") else None
    from kyraan.triggers.store import Reminder
    stale = Reminder(id="r1", chat_id=1, text="water",
                     when_iso=(local_now() - timedelta(days=3)).isoformat(),
                     repeat="daily")
    nxt = scheduler.advance_past_now(stale)
    assert nxt > local_now()
    assert nxt - local_now() <= timedelta(days=1)   # resumed at the NEXT slot, not later


def test_interval_reminder_catchup_respects_window():
    from kyraan.triggers.store import Reminder
    stale = Reminder(id="r2", chat_id=1, text="water",
                     when_iso=(local_now() - timedelta(days=2)).isoformat(),
                     repeat="interval", interval_minutes=60,
                     window_start="10:00", window_end="21:00")
    nxt = scheduler.advance_past_now(stale)
    assert nxt > local_now()


# --- P1: agent tasks — restart catch-up and one-shot survival --------------

@pytest.fixture
def task_env(monkeypatch):
    scheduled, sent = [], []

    def schedule_fn(job_id, when, payload):
        scheduled.append((job_id, when, payload))

    async def run_fn(chat_id, instruction):
        return "did the thing"

    async def send_fn(chat_id, text):
        sent.append(text)

    agent_tasks.init(schedule_fn, run_fn, send_fn)
    return scheduled, sent


async def test_stale_recurring_task_advances_past_now(task_env, monkeypatch):
    scheduled, _ = task_env
    task = agent_tasks.create(1, "check calendar and warn about early meetings",
                              (local_now() + timedelta(hours=1)).isoformat(), repeat="daily")
    # simulate 3 days of downtime
    agent_tasks._advance(task.id, (local_now() - timedelta(days=3)).isoformat())
    scheduled.clear()
    agent_tasks._schedule(next(t for t in agent_tasks.list_active() if t.id == task.id))
    assert scheduled and scheduled[-1][1] > local_now()
    stored = next(t for t in agent_tasks.list_active() if t.id == task.id)
    assert scheduler._parse_when(stored.when_iso) > local_now()  # base persisted
    agent_tasks.cancel(task.id)


async def test_oneshot_survives_dnd_and_failure_retires_on_success(task_env, monkeypatch):
    from kyraan.control_plane import kernel
    scheduled, sent = task_env
    task = agent_tasks.create(1, "one time: summarize tomorrow's calendar",
                              (local_now() + timedelta(minutes=5)).isoformat())

    # DND hold: still active, rescheduled
    monkeypatch.setattr(kernel, "can_send_proactively", lambda force=False: False)
    scheduled.clear()
    await agent_tasks.fire(task.id)
    assert any(t.id == task.id for t in agent_tasks.list_active())
    assert scheduled and scheduled[-1][1] > local_now()

    # transient failure: still active, retry scheduled
    monkeypatch.setattr(kernel, "can_send_proactively", lambda force=False: True)

    async def broken(chat_id, instruction):
        raise RuntimeError("model down")

    agent_tasks._run_fn = broken
    scheduled.clear()
    await agent_tasks.fire(task.id)
    assert any(t.id == task.id for t in agent_tasks.list_active())
    assert scheduled

    # success: runs, delivers, THEN retires
    async def works(chat_id, instruction):
        return "summary ready"

    agent_tasks._run_fn = works
    await agent_tasks.fire(task.id)
    assert sent and "summary ready" in sent[-1]
    assert not any(t.id == task.id for t in agent_tasks.list_active())


# --- P2: tasks reject interval repeats at scheduling time ------------------

async def test_task_schedule_refuses_interval(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel
    with pytest.raises(kernel.ToolFailed, match="use a reminder"):
        await loop_tools._task_schedule(1, {
            "instruction": "poll something repeatedly please",
            "when_iso": (local_now() + timedelta(hours=1)).isoformat(),
            "repeat": "interval"}, "")


# --- P2: photo kill-switch keeps the (reply, enroll) shape -----------------

async def test_photo_kill_switch_returns_the_tuple_shape(monkeypatch):
    from kyraan.agents import photo
    monkeypatch.setattr(photo.kill_switch, "is_engaged", lambda: True)
    reply, enroll = await photo.answer(9, "data:x", "hi")
    assert "kill switch" in reply.lower() and enroll is None


# --- P2: equal-score memories order newest-first ---------------------------

def test_equal_score_memories_newest_first(monkeypatch, tmp_path):
    from kyraan.memory import engine
    monkeypatch.setattr(engine, "active_entries", lambda: [
        {"id": "a", "content": "older twin", "created": "2026-01-01T00:00:00",
         "flags": [], "era": "", "importance": "normal", "kind": "fact"},
        {"id": "b", "content": "newer twin", "created": "2026-08-01T00:00:00",
         "flags": [], "era": "", "importance": "normal", "kind": "fact"},
    ], raising=False)
    context = engine.build_context("twin")
    assert context.index("newer twin") < context.index("older twin")


# --- P1: classifier fallback never leaks pending facts to a cloud tier -----

async def test_legacy_answer_keeps_pending_facts_off_cloud(monkeypatch):
    from kyraan.agents import legacy_handlers, orchestrator
    from kyraan.memory import store as memory_store

    monkeypatch.setattr(memory_store, "load_pending_facts",
                        lambda: "SECRET-PENDING: wife's surprise gift")
    captured = {}

    async def fake_acall(prompt="", system="", tier="", **kw):
        captured["system"] = system

        class _R:
            text = "ok"
        return _R()

    monkeypatch.setattr(legacy_handlers.router, "acall", fake_acall)
    # qa.answer's configured tier is frontier (cloud) in the shipped config
    await legacy_handlers._answer(90, "what should I buy?")
    assert "SECRET-PENDING" not in captured["system"]
    assert "held locally" in captured["system"]


# --- P2: privacy truths track the email-bodies opt-in ----------------------

def test_privacy_truth_flips_with_email_bodies(monkeypatch):
    from kyraan.agents.capabilities import capability_brief
    for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
              "GOOGLE_OAUTH_REFRESH_TOKEN"):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("KYRAAN_EMAIL_BODIES", raising=False)
    assert "email bodies are never read" in capability_brief()
    monkeypatch.setenv("KYRAAN_EMAIL_BODIES", "local")
    brief = capability_brief()
    assert "email bodies are never read" not in brief
    assert "never sent to any cloud service" in brief


# --- P1: voice detection locates the package without executing it ----------

def test_voice_available_uses_find_spec(monkeypatch):
    import importlib.util
    from kyraan.channels import voice
    calls = []
    real = importlib.util.find_spec

    def spy(name, *a):
        calls.append(name)
        return real(name, *a)

    monkeypatch.setattr(importlib.util, "find_spec", spy)
    voice.available()
    assert "mlx_whisper" in calls   # located, never imported here
