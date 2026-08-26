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
    own words — no template. (Email is excluded: its result deliberately
    short-circuits, see the privacy test below.)"""
    async def fake_dispatch(spec, args):
        assert spec.name == "home.get_state"
        return {"entity": "switch.ac", "state": "on", "name": "AC"}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    prompts = scripted_model([
        '{"action": "call", "tool": "home.get_state", "args": {"entity": "switch.ac"}}',
        '{"action": "reply", "text": "Yes — the AC is on."}',
    ])

    reply = await agent_loop.run(90, "is the AC on?")
    assert "AC is on" in reply
    assert "TOOL home.get_state" in prompts[1]   # the result reached the model
    assert '"on"' in prompts[1]


async def test_email_metadata_never_enters_a_cloud_prompt(scripted_model, monkeypatch):
    """§3a restored for the agent path (external review P1): with a cloud
    tier active, the email executor composes the reply in Python and
    short-circuits the loop — sender names and subjects appear in NO
    model prompt, and history stores a placeholder."""
    async def fake_dispatch(spec, args):
        return {"unread_estimate": 2, "messages": [
            {"from": '"Suman Das" <s@x.com>', "subject": "Invoice pending", "date": "d"}]}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    monkeypatch.setattr(orchestrator, "_cloud_tier_in_use", lambda: True)
    prompts = scripted_model([
        '{"action": "call", "tool": "email.unread", "args": {"limit": 5}}',
    ])

    token = orchestrator._history_redaction.set(None)
    try:
        reply = await agent_loop.run(90, "any new emails?")
        assert "Suman Das: Invoice pending" in reply       # the user sees it
        assert all("Invoice" not in p for p in prompts)    # no model ever did
        assert orchestrator._history_redaction.get() == "[showed the email.unread result]"
    finally:
        orchestrator._history_redaction.reset(token)


async def test_forgotten_facts_stay_forgotten_in_the_memory_block(monkeypatch, tmp_path):
    """External review P1: an existing-but-empty index must NOT fall back
    to the Markdown tree, which still holds forgotten facts' text."""
    from kyraan.memory import engine
    from kyraan.memory import store as mstore

    (mstore.MEMORY_ROOT / "people").mkdir(parents=True, exist_ok=True)
    (mstore.MEMORY_ROOT / "people" / "father.md").write_text("- Father's name is Deven Roy\n")

    # No index yet: migration fallback may show the tree.
    assert "Deven Roy" in agent_loop._memory_block("anything")

    engine.migrate_from_tree()
    fact_id = engine.active_entries()[0]["id"]
    engine.forget([fact_id])
    block = agent_loop._memory_block("who is my father?")
    assert "Deven Roy" not in block                        # forgotten stays forgotten


async def test_kill_switch_blocks_the_whole_loop(monkeypatch):
    """External review P1: reminders (and every other executor) must be
    unreachable with the kill switch engaged — checked at loop entry."""
    from kyraan.control_plane import kill_switch

    def must_not_call(**kwargs):
        raise AssertionError("no model call with the kill switch engaged")

    monkeypatch.setattr(agent_loop.router, "call", must_not_call)
    kill_switch.engage("test")
    try:
        with pytest.raises(kernel.KillSwitchEngaged):
            await agent_loop.run(90, "remind me to test at 9pm")
    finally:
        kill_switch.disengage()


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

    async def unavailable(chat_id, raw_text, tier="frontier"):
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


async def test_memory_forget_confirms_the_exact_facts_then_deactivates(scripted_model, monkeypatch):
    """G-11: forgetting is a confirm-gated write — the ask NAMES the
    matched facts, nothing deactivates before the yes, and the fact stays
    in the index as history."""
    from kyraan.memory import engine

    engine.add_fact("Father's name is Deven Roy", "people/father.md", "s")
    engine.add_fact("Wife's name is Mira", "people/wife.md", "s")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    scripted_model([
        '{"action": "call", "tool": "memory.forget", "args": {"fact": "father Deven Roy"}}',
    ])

    ask = await agent_loop.run(90, "forget the Deven Roy fact")
    assert "About to FORGET" in ask and "Deven Roy" in ask
    assert any(e["content"] == "Father's name is Deven Roy" for e in engine.active_entries())

    result = await orchestrator.handle_message(chat_id=90, raw_text="yes")
    assert "Forgotten" in result and "Deven Roy" in result
    active = [e["content"] for e in engine.active_entries()]
    assert "Father's name is Deven Roy" not in active
    assert "Wife's name is Mira" in active          # unrelated fact untouched
    assert any(e["content"] == "Father's name is Deven Roy" and not e["active"]
               for e in engine._load())             # history, not deletion


async def test_cheap_tier_loop_takes_over_when_frontier_is_down(monkeypatch):
    """G-02's core: degraded mode is the SAME brain on the local tier, not
    a different system."""
    tiers_tried = []

    async def tiered(chat_id, raw_text, tier="frontier"):
        tiers_tried.append(tier)
        if tier == "frontier":
            raise agent_loop.AgentUnavailable("cloud down")
        return "local loop reply"

    monkeypatch.setattr(orchestrator, "AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(agent_loop, "run", tiered)

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    result = await orchestrator.handle_message(chat_id=90, raw_text="hello")
    assert result.startswith("local loop reply")
    assert tiers_tried == ["frontier", "cheap"]


async def test_degraded_tier_carries_the_self_awareness_note(scripted_model):
    prompts = scripted_model(['{"action": "reply", "text": "Short answer."}'])
    systems = []

    # capture the system prompt via the scripted fake's kwargs
    import kyraan.agents.agent_loop as al
    original = al.router.call

    def capturing(prompt, system="", **kw):
        systems.append(system)
        return original(prompt, system=system, **kw)

    al.router.call = capturing
    try:
        await agent_loop.run(90, "hello", tier="cheap")
    finally:
        al.router.call = original
    assert "LOCAL backup model" in systems[0]


async def test_calendar_create_dispatches_the_normalized_time(scripted_model, monkeypatch):
    """External review P1: a model emitting 19:00Z for a 7 PM wall-clock
    intent validated fine but dispatched the raw Z string — 5.5 hours
    wrong in the user's home timezone. The NORMALIZED value must go out."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    dispatched = {}

    async def fake_dispatch(spec, args):
        dispatched.update(args)
        return {"id": "ev1", "link": "l", "title": args["title"]}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    monkeypatch.setattr(kernel.config, "skill_config",
                        lambda name: {"permission": "auto", "model_tier": "cheap"})
    scripted_model([
        '{"action": "call", "tool": "calendar.create_event", '
        '"args": {"title": "Call", "start": "2099-01-02T19:00:00Z", "end": "2099-01-02T20:00:00Z"}}',
    ])

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    ask = await agent_loop.run(90, "add call at 7pm on jan 2 2099")
    assert "About to create" in ask
    await orchestrator.handle_message(chat_id=90, raw_text="yes")
    assert dispatched["start"].endswith("+05:30")   # wall-clock kept, zone repaired
    assert "T19:00:00" in dispatched["start"]


async def test_confirm_ask_and_execution_use_the_same_anchored_time(scripted_model, monkeypatch):
    """PROPERTY (review P1): what the ask SHOWS is what the execution
    DOES — one normalization, including the stated-clock anchor ("8pm"
    beats the model's drifted timestamp)."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    dispatched = {}

    async def fake_dispatch(spec, args):
        dispatched.update(args)
        return {"id": "ev1", "link": "l", "title": args["title"]}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    monkeypatch.setattr(kernel.config, "skill_config",
                        lambda name: {"permission": "auto", "model_tier": "cheap"})
    scripted_model([
        '{"action": "call", "tool": "calendar.create_event", '
        '"args": {"title": "Call", "start": "2099-01-02T19:49:00Z", "end": "2099-01-02T20:49:00Z"}}',
    ])

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    ask = await agent_loop.run(90, "add a call at 8pm on jan 2 2099")
    assert "8:00 PM" in ask                              # the ask shows the anchored time
    await orchestrator.handle_message(chat_id=90, raw_text="yes")
    assert "T20:00:00" in dispatched["start"]            # and that is what executed
    assert dispatched["start"].endswith("+05:30")
    assert "T21:00:00" in dispatched["end"]              # duration preserved
