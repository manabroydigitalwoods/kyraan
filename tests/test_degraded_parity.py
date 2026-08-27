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

async def test_promise_and_narration_are_corrected(monkeypatch):
    _scripted(monkeypatch, [
        {"action": "reply", "consider": "…",
         "text": "I'll set a reminder to call your mom at 9:00 PM. Is that correct?"},
        {"action": "reply", "consider": "…",
         "text": "Let me check your pending reminders for you."},
        {"action": "reply", "consider": "honest now",
         "text": "I haven't set anything yet — say the word and I will."},
    ])
    events = []
    monkeypatch.setattr(agent_loop, "log_event",
                        lambda name, **kw: events.append((name, kw.get("violation", ""))))
    reply = await agent_loop.run(5, "set a reminder for mom", tier="cheap")
    assert "haven't set anything" in reply
    rails = [v for n, v in events if n == "agent_false_success_corrected"]
    assert len(rails) == 2 and "PROMISES" in rails[0] and "NARRATES" in rails[1]


async def test_memory_claim_is_corrected(monkeypatch):
    _scripted(monkeypatch, [
        {"action": "reply", "consider": "…",
         "text": "I've noted that your favourite snack is murukku. Is that correct?"},
        {"action": "reply", "consider": "plain ack", "text": "Murukku — nice choice!"},
    ])
    reply = await agent_loop.run(5, "my favourite snack is murukku", tier="cheap")
    assert reply == "Murukku — nice choice!"


def test_forget_purges_matching_pending_proposals():
    from kyraan.memory import engine
    from kyraan.memory import store as memory_store
    fid = engine.add_fact("Favourite fruit is dragonfruit", "preferences/f.md", "t")
    memory_store.propose_fact("preferences/f.md",
                              "Favourite fruit is dragonfruit", "restated")
    memory_store.propose_fact("preferences/g.md",
                              "Morning walk at 6am daily", "unrelated")
    engine.forget([fid])
    bodies = [p.read_text() for p in memory_store.PENDING_DIR.glob("*.md")]
    assert not any("dragonfruit" in b for b in bodies)   # purged with the fact
    assert any("Morning walk" in b for b in bodies)      # unrelated survives


async def test_has_been_set_claim_is_corrected(monkeypatch):
    _scripted(monkeypatch, [
        {"action": "reply", "consider": "…",
         "text": "Reminder to call mom at 9:00 PM has been set."},
        {"action": "reply", "consider": "honest", "text": "Nothing was set yet."},
    ])
    reply = await agent_loop.run(5, "set a reminder", tier="cheap")
    assert reply == "Nothing was set yet."


async def test_fabricated_listing_is_corrected(monkeypatch):
    _scripted(monkeypatch, [
        {"action": "reply", "consider": "…",
         "text": "Here are your pending reminders: 1. Drink water. 2. Check calendar."},
        {"action": "call", "tool": "reminders.list", "consider": "corrected",
         "args": {}},
        {"action": "reply", "consider": "real data",
         "text": "You have one reminder: call mom at 9:00 PM."},
    ])

    async def fake_list(chat_id, args, raw_text):
        return [{"id": "r1", "text": "call mom", "when": "9:00 PM"}]

    monkeypatch.setitem(agent_loop.TOOLS["reminders.list"], "run", fake_list)
    reply = await agent_loop.run(5, "any reminders?", tier="cheap")
    assert "call mom" in reply


async def test_real_listing_after_the_read_is_untouched(monkeypatch):
    _scripted(monkeypatch, [
        {"action": "call", "tool": "reminders.list", "consider": "…", "args": {}},
        {"action": "reply", "consider": "real",
         "text": "Here are your pending reminders: 1. call mom at 9:00 PM."},
    ])

    async def fake_list(chat_id, args, raw_text):
        return [{"id": "r1", "text": "call mom", "when": "9:00 PM"}]

    monkeypatch.setitem(agent_loop.TOOLS["reminders.list"], "run", fake_list)
    events = []
    monkeypatch.setattr(agent_loop, "log_event",
                        lambda name, **kw: events.append(name))
    reply = await agent_loop.run(5, "any reminders?", tier="cheap")
    assert "call mom" in reply
    assert "agent_false_success_corrected" not in events


async def test_listing_contradicting_the_read_is_corrected(monkeypatch):
    _scripted(monkeypatch, [
        {"action": "call", "tool": "reminders.list", "consider": "…", "args": {}},
        {"action": "reply", "consider": "fabricating",
         "text": "Here are your pending reminders:\n- Drink water every 5 minutes"},
        {"action": "reply", "consider": "grounded",
         "text": "Here are your pending reminders:\n- call mom at 9:00 PM"},
    ])

    async def fake_list(chat_id, args, raw_text):
        return [{"id": "r1", "text": "call mom", "when": "9:00 PM"}]

    monkeypatch.setitem(agent_loop.TOOLS["reminders.list"], "run", fake_list)
    reply = await agent_loop.run(5, "any reminders?", tier="cheap")
    assert "call mom" in reply and "Drink water" not in reply
