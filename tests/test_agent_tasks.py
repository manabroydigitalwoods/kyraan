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
