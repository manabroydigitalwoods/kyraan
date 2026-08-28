"""Scheduled agent tasks (harness pack A): storage, recurrence, DND
gating, and the loop's read-only enforcement."""
import pytest

from kyraan.triggers import agent_tasks


@pytest.fixture(autouse=True)
def isolated_tasks(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_tasks, "TASKS_PATH", tmp_path / "agent_tasks.json")


async def test_recurring_task_runs_and_advances(monkeypatch):
    from kyraan.control_plane import kernel

    monkeypatch.setattr(kernel, "can_send_proactively", lambda: True)
    scheduled, sends = [], []

    async def run_fn(chat_id, instruction):
        return f"ran: {instruction}"

    async def send_fn(chat_id, text):
        sends.append(text)

    agent_tasks.init(schedule_fn=lambda name, when, payload: scheduled.append(payload),
                     run_fn=run_fn, send_fn=send_fn)
    task = agent_tasks.create(1, "check tomorrow's calendar", "2099-01-01T20:00:00+05:30",
                              repeat="daily")
    scheduled.clear()
    await agent_tasks.fire(task.id)

    assert sends == ["⏱ ran: check tomorrow's calendar"]
    assert len(scheduled) == 1                                     # next occurrence armed
    assert agent_tasks.list_active(1)[0].when_iso.startswith("2099-01-02")

    # cancel ends the series
    assert agent_tasks.cancel(task.id) is True
    sends.clear()
    await agent_tasks.fire(task.id)
    assert sends == []


async def test_dnd_skips_the_occurrence_but_keeps_the_series(monkeypatch):
    from kyraan.control_plane import kernel

    monkeypatch.setattr(kernel, "can_send_proactively", lambda: False)
    scheduled, sends = [], []

    async def run_fn(chat_id, instruction):
        raise AssertionError("must not run during DND")

    async def send_fn(chat_id, text):
        sends.append(text)

    agent_tasks.init(schedule_fn=lambda name, when, payload: scheduled.append(when),
                     run_fn=run_fn, send_fn=send_fn)
    task = agent_tasks.create(1, "evening check", "2099-01-01T20:00:00+05:30", repeat="daily")
    scheduled.clear()
    await agent_tasks.fire(task.id)
    assert sends == []
    assert len(scheduled) == 1        # tomorrow still happens


async def test_one_shot_task_retires_after_running(monkeypatch):
    from kyraan.control_plane import kernel

    monkeypatch.setattr(kernel, "can_send_proactively", lambda: True)
    sends = []

    async def run_fn(chat_id, instruction):
        return "done"

    async def send_fn(chat_id, text):
        sends.append(text)

    agent_tasks.init(schedule_fn=lambda *a, **k: None, run_fn=run_fn, send_fn=send_fn)
    task = agent_tasks.create(1, "one time thing", "2099-01-01T20:00:00+05:30")
    await agent_tasks.fire(task.id)
    assert len(sends) == 1
    assert agent_tasks.list_active(1) == []


async def test_read_only_loop_blocks_writes(monkeypatch):
    """The safety core: a scheduled run cannot reach a write tool — not in
    the menu, and blocked if the model names one anyway."""
    from kyraan.agents import agent_loop

    block = agent_loop._tools_block(read_only=True)
    assert "calendar.delete_event" not in block
    assert "home.turn_on" not in block
    assert "calendar.list_events" in block

    calls = iter([
        '{"action": "call", "tool": "home.turn_off", "args": {"entity": "switch.ac"}}',
        '{"action": "reply", "text": "I can only report — ask me live to switch it."}',
    ])
    prompts = []

    def fake_call(prompt, system="", **kw):
        prompts.append(prompt)

        class R:
            text = next(calls)
        return R()

    monkeypatch.setattr(agent_loop.router, "call", fake_call)
    reply = await agent_loop.run(90, "turn off the ac", read_only=True)
    assert "ask me live" in reply
    assert "not available in a scheduled run" in prompts[1]


async def test_recurring_send_failure_redelivers_in_minutes(monkeypatch):
    """Bugbot round-2 P2: a recurring task's stashed result waited until
    the NEXT occurrence — days or weeks. A redeliver-only fire is now
    scheduled minutes out; it flushes the stash without advancing the
    series or running fresh work."""
    from datetime import timedelta

    from kyraan.control_plane import kernel
    from kyraan.control_plane.dnd import local_now
    from kyraan.triggers import agent_tasks

    scheduled = []
    sends = []
    state = {"fail": True}

    async def run_fn(chat_id, instruction):
        return "the result"

    async def send_fn(chat_id, text):
        if state["fail"]:
            raise RuntimeError("telegram down")
        sends.append(text)

    agent_tasks.init(
        schedule_fn=lambda name, when, payload: scheduled.append((name, payload)),
        run_fn=run_fn, send_fn=send_fn)
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: True)
    when = (local_now() + timedelta(days=7)).isoformat()
    task = agent_tasks.create(1, "weekly check", when, repeat="weekly")
    scheduled.clear()

    await agent_tasks.fire(task.id)                 # run ok, send fails
    redelivery = [(n, p) for n, p in scheduled if p.get("redeliver_only")]
    assert redelivery, "no redeliver-only job scheduled"
    stored = next(t for t in agent_tasks.list_active() if t.id == task.id)
    assert stored.pending_result == "the result"
    series_advances = [n for n, p in scheduled
                       if not p.get("redeliver_only")]

    state["fail"] = False
    await agent_tasks.fire(task.id, redeliver_only=True)
    assert sends and "the result" in sends[0]
    stored = next(t for t in agent_tasks.list_active() if t.id == task.id)
    assert stored.pending_result == ""              # flushed
    # the redeliver-only fire advanced NOTHING and ran nothing fresh
    assert [n for n, p in scheduled
            if not p.get("redeliver_only")] == series_advances
    assert len(sends) == 1


async def test_redelivery_survives_dnd_and_repeat_failures(monkeypatch):
    """Bugbot round-3 P2: the redeliver-only job consumed itself on a
    DND hold or a second failure, leaving the result stale until next
    week's occurrence. It now reschedules itself until delivered."""
    from datetime import timedelta

    from kyraan.control_plane import kernel
    from kyraan.control_plane.dnd import local_now
    from kyraan.triggers import agent_tasks

    scheduled = []
    state = {"dnd": False, "fail": True}
    sends = []

    async def run_fn(chat_id, instruction):
        return "weekly result"

    async def send_fn(chat_id, text):
        if state["fail"]:
            raise RuntimeError("telegram down")
        sends.append(text)

    agent_tasks.init(
        schedule_fn=lambda name, when, payload: scheduled.append((name, payload)),
        run_fn=run_fn, send_fn=send_fn)
    monkeypatch.setattr(kernel, "can_send_proactively",
                        lambda **kw: not state["dnd"])
    when = (local_now() + timedelta(days=7)).isoformat()
    task = agent_tasks.create(1, "weekly check", when, repeat="weekly")

    await agent_tasks.fire(task.id)                    # run ok, send fails
    def redeliveries():
        return [p for n, p in scheduled if p.get("redeliver_only")]
    assert len(redeliveries()) == 1

    # redelivery fire hits DND -> reschedules itself
    state["dnd"] = True
    await agent_tasks.fire(task.id, redeliver_only=True)
    assert len(redeliveries()) == 2

    # redelivery fire fails again -> reschedules itself
    state["dnd"] = False
    await agent_tasks.fire(task.id, redeliver_only=True)
    assert len(redeliveries()) == 3

    # finally delivers, stash cleared, no further redeliveries
    state["fail"] = False
    await agent_tasks.fire(task.id, redeliver_only=True)
    assert sends and "weekly result" in sends[0]
    assert len(redeliveries()) == 3
    stored = next(t for t in agent_tasks.list_active() if t.id == task.id)
    assert stored.pending_result == ""


async def test_boot_rearms_redelivery_for_stashed_results(monkeypatch):
    """Bugbot round-4 P2: the redeliver-only job lived only in the job
    queue's memory — a restart during backoff postponed the result to
    next week. init() now re-arms redelivery for any recurring task
    with a stashed result."""
    from datetime import timedelta

    from kyraan.control_plane.dnd import local_now
    from kyraan.triggers import agent_tasks

    scheduled = []

    async def noop(*a, **k):
        return ""

    agent_tasks.init(
        schedule_fn=lambda name, when, payload: scheduled.append((name, payload)),
        run_fn=noop, send_fn=noop)
    when = (local_now() + timedelta(days=7)).isoformat()
    task = agent_tasks.create(1, "weekly check", when, repeat="weekly")
    agent_tasks._set_pending_result(task.id, "stashed result")
    scheduled.clear()

    # simulate the restart: init() runs again over the persisted store
    agent_tasks.init(
        schedule_fn=lambda name, when, payload: scheduled.append((name, payload)),
        run_fn=noop, send_fn=noop)
    redeliveries = [p for n, p in scheduled if p.get("redeliver_only")]
    assert len(redeliveries) == 1 and redeliveries[0]["task_id"] == task.id
