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


# --- Audit round 2 ---------------------------------------------------------

async def test_empty_result_is_failure_not_success(task_env, monkeypatch):
    """Both tiers down surfaces as an empty result — the one-shot must
    survive it, not retire as if it ran."""
    from kyraan.control_plane import kernel
    scheduled, sent = task_env
    monkeypatch.setattr(kernel, "can_send_proactively", lambda force=False: True)
    task = agent_tasks.create(1, "summarize something once",
                              (local_now() + timedelta(minutes=5)).isoformat())

    async def empty(chat_id, instruction):
        return ""

    agent_tasks._run_fn = empty
    scheduled.clear()
    await agent_tasks.fire(task.id)
    assert any(t.id == task.id for t in agent_tasks.list_active())  # survived
    assert scheduled                                                # retry queued
    agent_tasks.cancel(task.id)


async def test_send_failure_never_reruns_the_model(task_env, monkeypatch):
    """Send failure: the run happened — the model NEVER runs again for
    delivery retries (round 3 upgraded retire-with-loss to
    stash-and-redeliver; this pins the no-duplicate-run half)."""
    from kyraan.control_plane import kernel
    scheduled, sent = task_env
    monkeypatch.setattr(kernel, "can_send_proactively", lambda force=False: True)
    task = agent_tasks.create(1, "summarize something once more",
                              (local_now() + timedelta(minutes=5)).isoformat())
    runs = []

    async def counted(chat_id, instruction):
        runs.append(1)
        return "the result"

    async def broken_send(chat_id, text):
        raise RuntimeError("connection reset mid-send")

    agent_tasks._run_fn = counted
    agent_tasks._send_fn = broken_send
    scheduled.clear()
    await agent_tasks.fire(task.id)
    await agent_tasks.fire(task.id)   # delivery retry with send still broken
    assert runs == [1]                # the model ran exactly once across both
    survivor = next(t for t in agent_tasks.list_active() if t.id == task.id)
    assert survivor.pending_result == "the result"
    agent_tasks.cancel(task.id)


def test_create_refuses_interval():
    with pytest.raises(ValueError, match="reminder feature"):
        agent_tasks.create(1, "poll this", (local_now() + timedelta(hours=1)).isoformat(),
                           repeat="interval")


def test_very_stale_interval_catchup_is_fast_and_future():
    from kyraan.triggers.store import Reminder
    import time as _time
    stale = Reminder(id="r3", chat_id=1, text="water",
                     when_iso=(local_now() - timedelta(days=400)).isoformat(),
                     repeat="interval", interval_minutes=5,
                     window_start="10:00", window_end="21:00")
    t0 = _time.monotonic()
    nxt = scheduler.advance_past_now(stale)
    assert _time.monotonic() - t0 < 0.5   # arithmetic, not 115k loop steps
    assert nxt > local_now()


async def test_local_fallback_rebuilds_prompt_with_pending_facts(monkeypatch):
    """The cheap-tier fallback must not inherit the cloud prompt's
    pending-facts placeholder — locally the facts are allowed."""
    from kyraan.agents import legacy_handlers
    from kyraan.memory import store as memory_store

    monkeypatch.setattr(memory_store, "load_pending_facts",
                        lambda: "PENDING-FACT: gift idea")
    monkeypatch.setattr(legacy_handlers.router, "provider_is_local",
                        lambda p: p == "ollama")
    systems = []

    call_count = {"n": 0}

    async def flaky_acall(prompt="", system="", tier="", **kw):
        systems.append((tier, system))
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise legacy_handlers.router.ModelProviderError("frontier down")

        class _R:
            text = "ok"
        return _R()

    monkeypatch.setattr(legacy_handlers.router, "acall", flaky_acall)
    await legacy_handlers._answer(90, "any gift ideas?")
    cloud_sys = systems[0][1]
    local_sys = systems[1][1]
    assert "PENDING-FACT" not in cloud_sys and "held locally" in cloud_sys
    assert "PENDING-FACT" in local_sys                 # rebuilt for local
    assert "LOCAL backup model" in local_sys           # degraded note intact


def test_voice_probe_runs_in_a_subprocess(monkeypatch):
    from kyraan.channels import voice
    voice._native_probe = None
    voice._probe_thread = None
    calls = {}

    class _Proc:
        returncode = 0

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return _Proc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda n: object())
    voice.available()                      # kicks the daemon-thread probe
    voice._probe_thread.join(timeout=2)
    assert voice.available() is True
    assert "import mlx_whisper" in " ".join(calls["cmd"])   # child, not us
    calls.clear()
    assert voice.available() is True and not calls          # cached
    voice._native_probe = None
    voice._probe_thread = None


# --- Audit round 3 ---------------------------------------------------------

def test_stale_interval_catchup_lands_inside_the_window():
    """The arithmetic jump must not resume a 10:00-21:00 series at 02:00."""
    from datetime import time as _dtime
    from kyraan.triggers.store import Reminder
    stale = Reminder(id="r4", chat_id=1, text="water",
                     when_iso=(local_now() - timedelta(days=90)).isoformat(),
                     repeat="interval", interval_minutes=60,
                     window_start="10:00", window_end="21:00")
    nxt = scheduler.advance_past_now(stale)
    assert nxt > local_now()
    assert _dtime(10, 0) <= nxt.time() <= _dtime(21, 0)


async def test_send_failure_stashes_result_and_redelivers(task_env, monkeypatch):
    """Round 3: a failed send stashes the produced result; the retry
    RESENDS it (labeled may-be-a-repeat) without re-running the model."""
    from kyraan.control_plane import kernel
    scheduled, sent = task_env
    monkeypatch.setattr(kernel, "can_send_proactively", lambda force=False: True)
    task = agent_tasks.create(1, "one shot with flaky delivery",
                              (local_now() + timedelta(minutes=5)).isoformat())
    runs = []

    async def counted(chat_id, instruction):
        runs.append(1)
        return "the precious result"

    async def broken_send(chat_id, text):
        raise RuntimeError("dns failure before anything was sent")

    agent_tasks._run_fn = counted
    agent_tasks._send_fn = broken_send
    await agent_tasks.fire(task.id)
    survivor = next(t for t in agent_tasks.list_active() if t.id == task.id)
    assert survivor.pending_result == "the precious result"   # not lost
    assert scheduler._parse_when(survivor.when_iso) > local_now()  # backoff persisted

    async def working_send(chat_id, text):
        sent.append(text)

    agent_tasks._send_fn = working_send
    await agent_tasks.fire(task.id)
    assert runs == [1]                                        # model ran ONCE
    assert "the precious result" in sent[-1] and "may be a repeat" in sent[-1]
    assert not any(t.id == task.id for t in agent_tasks.list_active())  # now retired


async def test_retry_backoff_survives_restart(task_env, monkeypatch):
    """Round 3: retry deadlines persist in when_iso, so a restart's
    _schedule honors the backoff instead of firing immediately."""
    from kyraan.control_plane import kernel
    scheduled, _ = task_env
    monkeypatch.setattr(kernel, "can_send_proactively", lambda force=False: True)
    task = agent_tasks.create(1, "flaky one shot",
                              (local_now() + timedelta(minutes=5)).isoformat())

    async def broken(chat_id, instruction):
        raise RuntimeError("model down")

    agent_tasks._run_fn = broken
    await agent_tasks.fire(task.id)
    stored = next(t for t in agent_tasks.list_active() if t.id == task.id)
    assert scheduler._parse_when(stored.when_iso) > local_now()  # persisted, not memory-only
    scheduled.clear()
    agent_tasks._schedule(stored)   # simulated restart
    assert scheduled and scheduled[-1][1] > local_now()          # backoff honored
    agent_tasks.cancel(task.id)


def test_voice_probe_does_not_block(monkeypatch):
    """Round 3: the probe runs in a daemon thread; until it reports,
    available() answers False fast instead of blocking the loop."""
    import time as _time
    from kyraan.channels import voice
    voice._native_probe = None
    voice._probe_thread = None
    started = {}

    class _SlowProc:
        returncode = 0

    def slow_run(cmd, **kw):
        started["yes"] = True
        _time.sleep(0.3)
        return _SlowProc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", slow_run)
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda n: object())
    t0 = _time.monotonic()
    first = voice.available()
    assert _time.monotonic() - t0 < 0.2    # returned before the probe finished
    assert first is False and started
    voice._probe_thread.join(timeout=2)
    assert voice.available() is True        # probe result landed
    voice._native_probe = None
    voice._probe_thread = None


# --- P2 pair (2026-08-27 round 2): window cadence + first voice note -------

def test_windowed_interval_catchup_preserves_the_grid(monkeypatch):
    """A 50-min 10:00-21:00 series has a daily grid of 10:00, 10:50,
    11:40, ... (every rollover re-anchors at window_start). Catch-up
    after downtime must resume ON that grid — continuous arithmetic
    resumed at 11:20 for a 'now' of 11:00 (Bugbot P2: intervals that
    don't divide the window evenly drifted)."""
    from datetime import datetime
    from kyraan.triggers.store import Reminder

    tz = local_now().tzinfo
    fake_now = datetime(2026, 9, 1, 11, 0, tzinfo=tz)
    monkeypatch.setattr(scheduler, "local_now", lambda: fake_now)
    stale = Reminder(id="g1", chat_id=1, text="water",
                     when_iso="2026-08-29T10:50:00+05:30",
                     repeat="interval", interval_minutes=50,
                     window_start="10:00", window_end="21:00")
    nxt = scheduler.advance_past_now(stale)
    assert (nxt.hour, nxt.minute) == (11, 40)   # the grid slot, not 11:20
    assert nxt.date() == fake_now.date()


def test_windowed_interval_catchup_edges(monkeypatch):
    """Before the window -> today's window start; after the last slot
    (including a grid step that crosses midnight) -> tomorrow's start."""
    from datetime import datetime
    from kyraan.triggers.store import Reminder

    tz = local_now().tzinfo
    stale = Reminder(id="g2", chat_id=1, text="water",
                     when_iso="2026-08-29T10:00:00+05:30",
                     repeat="interval", interval_minutes=50,
                     window_start="10:00", window_end="21:00")

    monkeypatch.setattr(scheduler, "local_now",
                        lambda: datetime(2026, 9, 1, 6, 0, tzinfo=tz))
    early = scheduler.advance_past_now(stale)
    assert (early.day, early.hour, early.minute) == (1, 10, 0)

    monkeypatch.setattr(scheduler, "local_now",
                        lambda: datetime(2026, 9, 1, 23, 59, tzinfo=tz))
    late = scheduler.advance_past_now(stale)
    assert (late.day, late.hour, late.minute) == (2, 10, 0)


async def test_first_voice_note_waits_for_the_probe(monkeypatch):
    """A healthy install must accept a voice note that arrives while the
    native probe is still running — wait_available joins the probe
    instead of reporting unavailable (Bugbot P2: the first voice note
    after startup was rejected)."""
    import threading
    import time as _time
    from kyraan.channels import voice

    monkeypatch.setattr(voice, "_native_probe", None)
    monkeypatch.setattr(voice, "_probe_thread", None)
    # CI has no mlx_whisper package: without this, find_spec returns None
    # and both calls bail before ever reaching the mocked probe
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda n: object())

    def slow_ok():
        _time.sleep(0.2)
        voice._native_probe = True

    monkeypatch.setattr(voice, "_run_probe", slow_ok)
    assert voice.available() is False          # probe just started: honest "not yet"
    assert await voice.wait_available() is True  # a note in hand waits and succeeds

    # and a genuinely broken install still comes back False
    monkeypatch.setattr(voice, "_native_probe", None)
    monkeypatch.setattr(voice, "_probe_thread", None)

    def slow_bad():
        _time.sleep(0.1)
        voice._native_probe = False

    monkeypatch.setattr(voice, "_run_probe", slow_bad)
    assert await voice.wait_available() is False


# --- P1/P2 round 3: explicit saves wait, same-day phase, stage depth -------

def test_same_day_windowed_catchup_keeps_the_records_phase(monkeypatch):
    """A 50-min 10:00-21:00 series whose next slot was 10:30 must resume
    on :30-phase slots after a short same-day outage — re-anchoring to
    window_start shifted the phase (Bugbot P2 round 3). Rollover-day
    catch-up still re-anchors at window_start, as a real rollover does."""
    from datetime import datetime
    from kyraan.triggers.store import Reminder
    from kyraan.control_plane.dnd import local_now as real_now

    tz = real_now().tzinfo
    fake_now = datetime(2026, 9, 1, 11, 0, tzinfo=tz)
    monkeypatch.setattr(scheduler, "local_now", lambda: fake_now)
    same_day = Reminder(id="p1", chat_id=1, text="water",
                        when_iso="2026-09-01T10:30:00+00:00",
                        repeat="interval", interval_minutes=50,
                        window_start="10:00", window_end="21:00")
    nxt = scheduler.advance_past_now(same_day)
    assert (nxt.hour, nxt.minute) == (11, 20)   # 10:30 + 50, not the 10:00 grid


async def test_explicit_save_survives_a_slow_local_model(monkeypatch):
    """'remember X' with a cold local model: the 6s bookkeeping timeout
    silently cancelled the user's actual request (Bugbot P1). An explicit
    save waits longer, and a timeout is now HONEST, never silent."""
    import asyncio
    from kyraan.agents import orchestrator

    async def instant_dispatch(chat_id, raw_text):
        return "Okay."

    monkeypatch.setattr(orchestrator, "_dispatch", instant_dispatch)

    async def slow_note(chat_id, raw_text):
        await asyncio.sleep(8)          # slower than the implicit 6s cutoff
        return "\n\n📝 Noted for review: sister works at the hospital."

    monkeypatch.setattr(orchestrator, "_extraction_note", slow_note)
    reply = await orchestrator.handle_message(1, "remember that my sister works at the hospital")
    assert "Noted for review" in reply   # waited past 6s instead of cancelling

    async def never_note(chat_id, raw_text):
        await asyncio.sleep(3600)

    monkeypatch.setattr(orchestrator, "_extraction_note", never_note)
    monkeypatch.setattr(orchestrator, "_SAVE_WORDS", orchestrator._SAVE_WORDS)
    # shrink the explicit ceiling so the test doesn't wait 45s for the
    # honesty path — the property is the message, not the exact number
    real_wait_for = asyncio.wait_for

    async def tiny_ceiling(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=0.05)

    monkeypatch.setattr(asyncio, "wait_for", tiny_ceiling)
    reply = await orchestrator.handle_message(1, "remember that my sister works at the hospital")
    assert "Nothing was saved" in reply  # honest, not silent


def test_nested_stages_do_not_double_count():
    """The extraction stage CONTAINS its model calls: nested stage
    records carry depth>0 so trace math counts only top-level time
    (Bugbot P2: percentages exceeded 100%)."""
    from kyraan.control_plane import logging_setup as ls

    ls.new_turn()
    with ls.stage("outer"):
        with ls.stage("inner"):
            pass
        ls.record_stage("model:test", 5.0)
    ls.record_stage("model:toplevel", 3.0)
    stages = {s["stage"]: s["depth"] for s in ls.turn_summary()["stages"]}
    assert stages["outer"] == 0
    assert stages["inner"] == 1
    assert stages["model:test"] == 1     # direct record inside the block
    assert stages["model:toplevel"] == 0


# --- round 4: long intervals keep their date; save-words need intent -------

def test_multiday_windowed_interval_keeps_date_and_phase(monkeypatch):
    """A weekly series (Monday 10:30, window 10:00-21:00) that missed a
    fire must resume NEXT Monday 10:30 — the daily-grid shortcut
    re-anchored it to 'today's window start', changing its weekday and
    phase (Bugbot P1 round 4). The grid is only for series that cycle
    within a day."""
    from datetime import datetime
    from kyraan.triggers.store import Reminder
    from kyraan.control_plane.dnd import local_now as real_now

    tz = real_now().tzinfo
    # Monday 2026-09-07; downtime since the fire scheduled Mon 2026-08-31
    fake_now = datetime(2026, 9, 3, 15, 0, tzinfo=tz)   # a Thursday
    monkeypatch.setattr(scheduler, "local_now", lambda: fake_now)
    weekly = Reminder(id="w1", chat_id=1, text="water the big plants",
                      when_iso="2026-08-31T10:30:00+00:00",  # a Monday
                      repeat="interval", interval_minutes=7 * 24 * 60,
                      window_start="10:00", window_end="21:00")
    nxt = scheduler.advance_past_now(weekly)
    assert nxt.weekday() == 0                      # still a Monday
    assert (nxt.hour, nxt.minute) == (10, 30)      # still :30 phase
    assert nxt > fake_now


def test_explicit_save_needs_intent_not_substrings():
    """'How can I save time?' matched the raw save-words and inherited
    the 45s extraction ceiling (Bugbot P2 round 4). The detector wants an
    instruction with an object; questions and 'you remember' are out."""
    from kyraan.agents.orchestrator import is_explicit_save

    assert is_explicit_save("remember that my sister works at the hospital")
    assert is_explicit_save("save this: the wifi password hint is the dog")
    assert is_explicit_save("note down my blood group is O+")
    assert is_explicit_save("please keep in mind I hate okra")

    assert not is_explicit_save("How can I save time?")
    assert not is_explicit_save("do you remember my birthday?")
    assert not is_explicit_save("can you save it as a draft?")
    assert not is_explicit_save("will you remember this tomorrow?")
    assert not is_explicit_save("I need to save money this month")


def test_explicit_save_keeps_the_live_phrasings():
    """The owner's real phrasings from the live incidents must stay
    classified as saves — the detector tightened, not the contract."""
    from kyraan.agents.orchestrator import is_explicit_save

    assert is_explicit_save("save the aarav age")
    assert is_explicit_save("you should save tarun name")


# --- round 5: window-length boundary, polite saves, receipt history --------

def test_interval_longer_than_the_window_keeps_advance_for_semantics(monkeypatch):
    """A 23h step in an 11h window overflows EVERY time, so the true rule
    collapses to 'daily at window_start' — the daily-grid shortcut
    computed window_start + k*23h and fired hours early (Bugbot P1 round
    5). The boundary is the WINDOW length, not 24h."""
    from datetime import datetime
    from kyraan.triggers.store import Reminder
    from kyraan.control_plane.dnd import local_now as real_now

    tz = real_now().tzinfo
    fake_now = datetime(2026, 9, 3, 15, 0, tzinfo=tz)
    monkeypatch.setattr(scheduler, "local_now", lambda: fake_now)
    rec = Reminder(id="l1", chat_id=1, text="long series",
                   when_iso="2026-08-30T10:30:00+00:00",
                   repeat="interval", interval_minutes=23 * 60,
                   window_start="10:00", window_end="21:00")
    nxt = scheduler.advance_past_now(rec)

    # ground truth: iterate the real rule from the record's own base
    truth = scheduler.advance_for(rec)
    while truth <= fake_now:
        truth = scheduler.advance_for(rec, from_when=truth)
    assert nxt == truth
    assert nxt > fake_now


def test_short_interval_still_uses_the_fast_grid(monkeypatch):
    """The optimization must survive the boundary change: a 50-min series
    inside an 11h window still lands on the grid, in one jump."""
    from datetime import datetime
    from kyraan.triggers.store import Reminder
    from kyraan.control_plane.dnd import local_now as real_now

    tz = real_now().tzinfo
    fake_now = datetime(2026, 9, 1, 11, 0, tzinfo=tz)
    monkeypatch.setattr(scheduler, "local_now", lambda: fake_now)
    rec = Reminder(id="l2", chat_id=1, text="water",
                   when_iso="2026-09-01T10:30:00+00:00",
                   repeat="interval", interval_minutes=50,
                   window_start="10:00", window_end="21:00")
    assert (scheduler.advance_past_now(rec).hour,
            scheduler.advance_past_now(rec).minute) == (11, 20)


def test_polite_saves_and_noun_phrases():
    """'Can you remember that...' is a polite COMMAND (was rejected with
    the recall questions); 'This note contains...' is prose (was a false
    positive) — Bugbot P1 round 5."""
    from kyraan.agents.orchestrator import is_explicit_save

    assert is_explicit_save("Can you remember that I switched to the new bank")
    assert is_explicit_save("could you note my new address is 4 Park Lane")
    assert is_explicit_save("please save that my passport expires in March")

    assert not is_explicit_save("This note contains the recipe for the cake")
    assert not is_explicit_save("the note says we should leave early")
    assert not is_explicit_save("do you remember my birthday")   # recall, no '?'

    # round-4 contracts unchanged
    assert is_explicit_save("save the aarav age")
    assert is_explicit_save("you should save tarun name")
    assert not is_explicit_save("How can I save time?")
    assert not is_explicit_save("I need to save money this month")


async def test_cancel_receipt_stays_in_history(monkeypatch, tmp_path):
    """A cancel receipt names the owner's own reminder — nothing private
    — so history keeps it verbatim; a generic '[showed the ... result]'
    left a follow-up unable to tell WHICH reminder went (Bugbot P2 r5).
    The email boundary's placeholder behavior is unchanged."""
    from kyraan.agents import agent_loop, loop_tools, orchestrator
    from kyraan.triggers import scheduler as sch, store as rstore

    monkeypatch.setattr(rstore, "REMINDERS_PATH", tmp_path / "reminders.json")
    sch.init(schedule_fn=lambda *a, **k: None,
             cancel_fn=lambda *a, **k: None, send_fn=None)
    r = sch.create_reminder(93, "Call mom", "2099-01-01T21:00:00+05:30")

    class _R:
        def __init__(self, t): self.text = t

    decisions = iter([
        f'{{"action": "call", "tool": "reminders.cancel", '
        f'"args": {{"reminder_id": "{r.id[:8]}"}}}}',
    ])
    monkeypatch.setattr(agent_loop.router, "call",
                        lambda prompt, system="", **kw: _R(next(decisions)))
    token = orchestrator._history_redaction.set(None)
    try:
        reply = await agent_loop.run(93, "cancel my reminder")
        assert "Call mom" in orchestrator._history_redaction.get()
        assert orchestrator._history_redaction.get() == reply
    finally:
        orchestrator._history_redaction.reset(token)


async def test_email_direct_reply_still_redacts_history(monkeypatch):
    """The privacy default is untouched: an executor that does NOT opt in
    still gets the blind placeholder."""
    from kyraan.agents import agent_loop, orchestrator
    from kyraan.tools import registry as reg

    async def fake_dispatch(spec, args):
        return {"unread_estimate": 2, "messages": [
            {"from": "Bank <b@x.com>", "subject": "Statement ready"}]}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    monkeypatch.setattr(orchestrator, "_cloud_tier_in_use", lambda: True)

    class _R:
        def __init__(self, t): self.text = t

    decisions = iter(['{"action": "call", "tool": "email.unread", "args": {}}'])
    monkeypatch.setattr(agent_loop.router, "call",
                        lambda prompt, system="", **kw: _R(next(decisions)))
    token = orchestrator._history_redaction.set(None)
    try:
        reply = await agent_loop.run(94, "any new emails?")
        assert "Statement ready" in reply                      # the owner sees it
        assert "Statement ready" not in orchestrator._history_redaction.get()
        assert "[showed the email.unread result]" == orchestrator._history_redaction.get()
    finally:
        orchestrator._history_redaction.reset(token)
