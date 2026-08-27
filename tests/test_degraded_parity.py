"""P3.7a — the two degraded-mode fixes: the false-success rail (a reply
claiming a write that never ran forces one re-decide) and the widened
extraction cutoff on cheap-tier turns."""
import json

import pytest

from kyraan.agents import agent_loop, orchestrator


def _scripted(monkeypatch, decisions):
    payloads = list(decisions)

    async def fake_acall(**kwargs):
        class R:
            text = json.dumps(payloads.pop(0))
        return R()

    monkeypatch.setattr(agent_loop.router, "acall", fake_acall)


async def test_false_claim_forces_the_tool_call(monkeypatch):
    _scripted(monkeypatch, [
        {"action": "reply", "consider": "…",
         "text": "I've set a reminder to call your mom at 9:00 PM today."},
        {"action": "call", "tool": "reminders.create", "consider": "corrected",
         "args": {"text": "call mom", "when_iso": "2027-01-01T21:00:00+05:30"}},
        {"action": "reply", "consider": "done",
         "text": "Done — I'll remind you to call mom at 9:00 PM."},
    ])
    created = []

    async def fake_create(chat_id, args, raw_text):
        created.append(dict(args))
        return {"created": True, "id": "r1", "text": args["text"], "when": "9pm"}

    monkeypatch.setitem(agent_loop.TOOLS["reminders.create"], "run", fake_create)
    from kyraan.store import actions
    monkeypatch.setattr(actions, "record", lambda *a, **k: "aid")
    reply = await agent_loop.run(5, "remind me to call mom at 9pm", tier="cheap")
    assert created and created[0]["text"] == "call mom"   # the write REALLY ran
    assert "Done — I'll remind you" in reply              # claim now true


async def test_honest_admission_stands_on_the_second_decide(monkeypatch):
    _scripted(monkeypatch, [
        {"action": "reply", "consider": "…",
         "text": "I've scheduled the task for tonight."},
        {"action": "reply", "consider": "admitting",
         "text": "I haven't actually scheduled anything yet — tell me the time and I will."},
    ])
    reply = await agent_loop.run(5, "hm", tier="cheap")
    assert "haven't actually scheduled" in reply


async def test_true_claim_after_real_write_passes_untouched(monkeypatch):
    _scripted(monkeypatch, [
        {"action": "call", "tool": "reminders.create", "consider": "…",
         "args": {"text": "water", "when_iso": "2027-01-01T21:00:00+05:30"}},
        {"action": "reply", "consider": "done",
         "text": "Done — I'll remind you to water at 9:00 PM."},
    ])

    async def fake_create(chat_id, args, raw_text):
        return {"created": True, "id": "r1", "text": args["text"], "when": "9pm"}

    monkeypatch.setitem(agent_loop.TOOLS["reminders.create"], "run", fake_create)
    from kyraan.store import actions
    monkeypatch.setattr(actions, "record", lambda *a, **k: "aid")
    events = []
    monkeypatch.setattr(agent_loop, "log_event",
                        lambda name, **kw: events.append(name))
    reply = await agent_loop.run(5, "remind me to water at 9pm", tier="cheap")
    assert "Done" in reply
    assert "agent_false_success_corrected" not in events


async def test_plain_reply_with_no_claim_is_untouched(monkeypatch):
    _scripted(monkeypatch, [
        {"action": "reply", "consider": "…", "text": "The capital of France is Paris."},
    ])
    assert "Paris" in await agent_loop.run(5, "capital of France?", tier="cheap")


# --- the degraded extraction window ---------------------------------------

def test_extraction_timeout_widens_only_on_degraded_turns():
    assert orchestrator._extraction_timeout(explicit_save=True) == 45
    assert orchestrator._extraction_timeout(explicit_save=False) == 6
    token = orchestrator._degraded_turn.set(True)
    try:
        assert orchestrator._extraction_timeout(explicit_save=False) == 30
        assert orchestrator._extraction_timeout(explicit_save=True) == 45
    finally:
        orchestrator._degraded_turn.reset(token)


async def test_cheap_fallback_marks_the_turn_degraded(monkeypatch):
    monkeypatch.setattr(orchestrator, "AGENT_LOOP_ENABLED", True)
    seen = {}

    async def fake_run(chat_id, raw_text, tier):
        if tier == "frontier":
            raise agent_loop.AgentUnavailable("cloud down")
        seen["degraded"] = orchestrator._degraded_turn.get()
        return "from cheap"

    monkeypatch.setattr(agent_loop, "run", fake_run)
    reply = await orchestrator._dispatch(940_001, "what's the capital of France?")
    assert reply == "from cheap"
    assert seen["degraded"] is True