"""The model-driven tool loop — decision protocol, read-chaining, the
confirm gate on writes, and every path that falls back to the classifier.
The model is scripted; kernel gating is real (adapters faked at
registry.dispatch, same as the calendar tests)."""
from dataclasses import dataclass

import pytest

from kyraan.agents import agent_loop, orchestrator
from kyraan.control_plane import kernel
from kyraan.tools import registry as reg


@dataclass
class _FakeRouted:
    text: str


@pytest.fixture
def scripted_model(monkeypatch):
    """Feed the loop a fixed sequence of decisions; capture each prompt."""
    prompts = []

    def install(decisions):
        it = iter(decisions)

        def fake_call(prompt, system="", **kwargs):
            prompts.append(prompt)
            return _FakeRouted(text=next(it))

        monkeypatch.setattr(agent_loop.router, "call", fake_call)
        return prompts

    return install


@pytest.fixture(autouse=True)
def clean_chat_state():
    orchestrator._history.pop(90, None)
    orchestrator._pending_confirmations.pop(90, None)
    yield
    orchestrator._history.pop(90, None)
    orchestrator._pending_confirmations.pop(90, None)


async def test_plain_conversation_replies_without_tools(scripted_model, monkeypatch):
    dispatched = []

    async def no_dispatch(spec, args):
        dispatched.append(spec.name)

    monkeypatch.setattr(reg, "dispatch", no_dispatch)
    scripted_model(['{"action": "reply", "text": "Hello Manab! How can I help?"}'])

    reply = await agent_loop.run(90, "hello")
    assert reply == "Hello Manab! How can I help?"
    assert dispatched == []


async def test_read_chain_result_feeds_the_next_decision(scripted_model, monkeypatch):
    """The loop's whole point: call, SEE the result, then answer in its
    own words — no template."""
    async def fake_dispatch(spec, args):
        assert spec.name == "email.unread"
        return {"unread_estimate": 2, "messages": [
            {"from": "Suman", "subject": "Invoice", "date": "d"}]}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    prompts = scripted_model([
        '{"action": "call", "tool": "email.unread", "args": {"limit": 5}}',
        '{"action": "reply", "text": "2 unread — the latest is Suman about an invoice."}',
    ])

    reply = await agent_loop.run(90, "any new emails?")
    assert "Suman" in reply
    assert "TOOL email.unread" in prompts[1]   # the result reached the model
    assert "Invoice" in prompts[1]


async def test_write_asks_first_and_yes_replays_the_exact_call(scripted_model, monkeypatch):
    """The kernel confirm gate, end to end through the loop: the delete
    does NOT run at decision time; the ask names the event; the owner's
    yes replays the stashed call byte-identical."""
    dispatched = []

    async def fake_dispatch(spec, args):
        dispatched.append((spec.name, args))
        return {"id": args.get("event_id"), "deleted": True, "already_gone": False}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    scripted_model([
        '{"action": "call", "tool": "calendar.delete_event", '
        '"args": {"event_id": "ev9", "title": "Test Event"}}',
    ])

    ask = await agent_loop.run(90, "cancel the test event")
    assert "About to DELETE" in ask and "Test Event" in ask and "yes" in ask
    assert dispatched == []  # nothing deleted before the yes

    result = await orchestrator.handle_message(chat_id=90, raw_text="yes")
    assert dispatched == [("calendar.delete_event", {"event_id": "ev9", "title": "Test Event"})]
    assert "Deleted from your calendar" in result and "Test Event" in result


async def test_tool_error_is_shown_to_the_model_not_the_user(scripted_model, monkeypatch):
    async def broken_dispatch(spec, args):
        raise reg.ToolError("feed unreachable")

    monkeypatch.setattr(reg, "dispatch", broken_dispatch)
    prompts = scripted_model([
        '{"action": "call", "tool": "email.unread", "args": {}}',
        '{"action": "reply", "text": "I couldn\'t reach email just now — the feed is unreachable."}',
    ])

    reply = await agent_loop.run(90, "check emails")
    assert "unreachable" in reply
    assert '"error"' in prompts[1]  # the model saw the failure and answered honestly


async def test_malformed_decision_gets_one_retry_then_unavailable(scripted_model):
    scripted_model(["not json at all",
                    '{"action": "reply", "text": "Recovered."}'])
    assert await agent_loop.run(90, "hi") == "Recovered."

    scripted_model(["still not json", "worse"])
    with pytest.raises(agent_loop.AgentUnavailable):
        await agent_loop.run(90, "hi")


async def test_unknown_tool_counts_as_malformed(scripted_model):
    scripted_model([
        '{"action": "call", "tool": "rockets.launch", "args": {}}',
        '{"action": "reply", "text": "Sticking to what I have."}',
    ])
    assert await agent_loop.run(90, "launch") == "Sticking to what I have."


async def test_provider_outage_raises_unavailable(monkeypatch):
    def down(**kwargs):
        raise agent_loop.router.ModelProviderError("openai down")

    monkeypatch.setattr(agent_loop.router, "call", down)
    with pytest.raises(agent_loop.AgentUnavailable):
        await agent_loop.run(90, "hello")


async def test_orchestrator_falls_back_to_the_classifier_path(monkeypatch):
    """AgentUnavailable = degraded mode: the proven classifier path takes
    over, invisible to the user."""
    from kyraan.intent.normalize import NormalizedIntent

    async def unavailable(chat_id, raw_text):
        raise agent_loop.AgentUnavailable("down")

    monkeypatch.setattr(orchestrator, "AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(agent_loop, "run", unavailable)
    monkeypatch.setattr(orchestrator, "normalize", lambda raw_text, tier="cheap", history="":
                        NormalizedIntent(intent="qa.answer", confidence=1.0, normalized_text=raw_text))

    async def fake_answer(chat_id, text):
        return "Classifier fallback answered."

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator, "_answer", fake_answer)
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    result = await orchestrator.handle_message(chat_id=90, raw_text="hello")
    assert result.startswith("Classifier fallback answered.")


async def test_step_cap_raises_unavailable(scripted_model, monkeypatch, tmp_path):
    from kyraan.triggers import store as rstore

    monkeypatch.setattr(rstore, "REMINDERS_PATH", tmp_path / "reminders.json")
    scripted_model(['{"action": "call", "tool": "reminders.list", "args": {}}'] * 6)
    with pytest.raises(agent_loop.AgentUnavailable):
        await agent_loop.run(90, "loop forever")


async def test_repeated_identical_call_gets_nudged_then_cut_off(scripted_model, monkeypatch, tmp_path):
    """Seen live: usage.report called 5x in a row past its own results,
    burning the step cap. Second identical call gets a system nudge to
    reply; a third raises AgentUnavailable (classifier beats a stuck loop)."""
    from kyraan.triggers import store as rstore

    monkeypatch.setattr(rstore, "REMINDERS_PATH", tmp_path / "reminders.json")
    same = '{"action": "call", "tool": "reminders.list", "args": {}}'

    prompts = scripted_model([same, same,
                              '{"action": "reply", "text": "No reminders pending."}'])
    reply = await agent_loop.run(90, "any reminders?")
    assert reply == "No reminders pending."
    assert "already called reminders.list" in prompts[2]  # the nudge landed

    scripted_model([same, same, same])
    with pytest.raises(agent_loop.AgentUnavailable, match="stuck"):
        await agent_loop.run(90, "any reminders?")


async def test_bad_days_value_falls_back_to_default(monkeypatch, tmp_path):
    """days='few days' crashed the executor live; any junk now means 7."""
    from kyraan.control_plane import logging_setup
    from kyraan.model_router import router as r

    monkeypatch.setattr(logging_setup, "EVENT_LOG", tmp_path / "e.jsonl")
    monkeypatch.setattr(r, "COST_LEDGER_PATH", tmp_path / "ledger.json")
    result = await agent_loop.TOOLS["usage.report"]["run"](90, {"days": "few days"}, "usage?")
    assert "budget" in result and isinstance(result["days"], list)


async def test_executor_errors_reach_the_model_verbatim(scripted_model, monkeypatch):
    """The model can only self-correct if it sees the REAL error."""
    async def broken(chat_id, args, raw_text):
        raise ValueError("invalid literal for int() with base 10: 'few days'")

    monkeypatch.setattr(agent_loop.TOOLS["usage.report"], "run", broken) if False else None
    original = agent_loop.TOOLS["usage.report"]["run"]
    agent_loop.TOOLS["usage.report"]["run"] = broken
    try:
        prompts = scripted_model([
            '{"action": "call", "tool": "usage.report", "args": {"days": "few days"}}',
            '{"action": "reply", "text": "Let me redo that with a number."}',
        ])
        await agent_loop.run(90, "usage last few days")
        assert "invalid literal" in prompts[1]
    finally:
        agent_loop.TOOLS["usage.report"]["run"] = original
