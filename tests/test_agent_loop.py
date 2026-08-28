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
    agent_loop._listing_cache.pop(90, None)
    yield
    orchestrator._history.pop(90, None)
    orchestrator._pending_confirmations.pop(90, None)
    agent_loop._listing_cache.pop(90, None)


async def test_plain_conversation_replies_without_tools(scripted_model, monkeypatch):
    dispatched = []

    async def no_dispatch(spec, args):
        dispatched.append(spec.name)

    monkeypatch.setattr(reg, "dispatch", no_dispatch)
    scripted_model(['{"action": "reply", "text": "Hello Arun! How can I help?"}'])

    reply = await agent_loop.run(90, "hello")
    assert reply == "Hello Arun! How can I help?"
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
            {"from": '"Rohan Sen" <s@x.com>', "subject": "Invoice pending", "date": "d"}]}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    monkeypatch.setattr(orchestrator, "_cloud_tier_in_use", lambda: True)
    prompts = scripted_model([
        '{"action": "call", "tool": "email.unread", "args": {"limit": 5}}',
    ])

    token = orchestrator._history_redaction.set(None)
    try:
        reply = await agent_loop.run(90, "any new emails?")
        assert "Rohan Sen: Invoice pending" in reply       # the user sees it
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
    (mstore.MEMORY_ROOT / "people" / "father.md").write_text("- Father's name is Deven Rao\n")

    # No index yet: migration fallback may show the tree.
    assert "Deven Rao" in agent_loop._memory_block("anything")

    engine.migrate_from_tree()
    fact_id = engine.active_entries()[0]["id"]
    engine.forget([fact_id])
    block = agent_loop._memory_block("who is my father?")
    assert "Deven Rao" not in block                        # forgotten stays forgotten


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
    # deletion requires an id from THIS conversation's listing (security
    # round P1) — seed it as a prior calendar.list_events would have
    import time as _time
    agent_loop._listing_cache[90] = {"at": _time.monotonic(), "items": {"ev9": "Test Event"}}
    scripted_model([
        '{"action": "call", "tool": "calendar.delete_event", '
        '"args": {"event_id": "ev9", "title": "Test Event"}}',
    ])

    ask = await agent_loop.run(90, "cancel the test event")
    assert "About to DELETE" in ask and "Test Event" in ask and "yes" in ask
    assert dispatched == []  # nothing deleted before the yes

    result = await orchestrator.handle_message(chat_id=90, raw_text="yes")
    # the confirmed replay first OBSERVES the event (prior capture — the
    # delete is undoable since the P3.1d completion), then deletes
    assert dispatched == [
        ("calendar.get_event", {"event_id": "ev9"}),
        ("calendar.delete_event", {"event_id": "ev9", "title": "Test Event"})]
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

    engine.add_fact("Father's name is Deven Rao", "people/father.md", "s")
    engine.add_fact("Wife's name is Mira", "people/wife.md", "s")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    scripted_model([
        '{"action": "call", "tool": "memory.forget", "args": {"fact": "father Deven Rao"}}',
    ])

    ask = await agent_loop.run(90, "forget the Deven Rao fact")
    assert "About to FORGET" in ask and "Deven Rao" in ask
    assert any(e["content"] == "Father's name is Deven Rao" for e in engine.active_entries())

    result = await orchestrator.handle_message(chat_id=90, raw_text="yes")
    assert "Forgotten" in result and "Deven Rao" in result
    active = [e["content"] for e in engine.active_entries()]
    assert "Father's name is Deven Rao" not in active
    assert "Wife's name is Mira" in active          # unrelated fact untouched
    assert any(e["content"] == "Father's name is Deven Rao" and not e["active"]
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


async def test_time_range_anchors_both_ends_and_the_receipt_matches(scripted_model, monkeypatch):
    """Review P1+P2: "8pm to 9pm" gave TWO clock matches and zero
    anchoring; now both ends anchor, and the success receipt shows the
    EXECUTED time, not the raw model timestamp."""
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
        '"args": {"title": "Dinner", "start": "2099-01-02T19:49:00Z", "end": "2099-01-02T21:12:00Z"}}',
    ])

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    ask = await agent_loop.run(90, "add dinner from 8pm to 9pm on jan 2 2099")
    assert "8:00 PM" in ask and "9:00 PM" in ask
    receipt = await orchestrator.handle_message(chat_id=90, raw_text="yes")
    assert "T20:00:00" in dispatched["start"] and "T21:00:00" in dispatched["end"]
    assert "8:00 PM" in receipt            # the receipt shows what executed
    assert "7:49" not in receipt


def test_unrelated_time_pair_does_not_hijack_the_event_range(monkeypatch):
    """Round-4 P2: any two AM/PM values are NOT automatically the range —
    a non-chronological anchor result means they weren't, and the parsed
    model times stand."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    args = {"start": "2099-01-02T20:00:00+05:30", "end": "2099-01-02T21:00:00+05:30"}
    # "9pm" then "8am" would anchor start->21:00, end->08:00 — reversed.
    start, end = agent_loop._normalized_event_times(
        args, "after the 9pm call tomorrow, add dinner — not at 8am obviously")
    assert "T20:00:00" in start and "T21:00:00" in end  # model times kept


def test_anchor_tolerance_rejects_hours_away_hijacks(monkeypatch):
    """Round-5 P2: anchoring corrects minutes of drift, never hours — an
    unrelated time in the message ("after my 9am meeting...") must not
    drag the event across the day. And end<=start is refused outright."""
    import pytest
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")

    # single unrelated time, hours away: model values stand
    args = {"start": "2099-01-02T20:00:00+05:30", "end": "2099-01-02T21:00:00+05:30"}
    start, end = agent_loop._normalized_event_times(
        args, "after my 9am meeting, add dinner tomorrow evening")
    assert "T20:00:00" in start and "T21:00:00" in end

    # minutes of drift: still corrected
    drift = {"start": "2099-01-02T19:49:00+05:30", "end": "2099-01-02T20:49:00+05:30"}
    start, end = agent_loop._normalized_event_times(drift, "dinner at 8pm please")
    assert "T20:00:00" in start

    # chronological but unrelated PAIR, hours away: rejected
    start, end = agent_loop._normalized_event_times(
        args, "my 9am meeting ran long, and the 10am too — dinner as planned")
    assert "T20:00:00" in start and "T21:00:00" in end

    # a model range with end <= start is refused, never dispatched
    bad = {"start": "2099-01-02T21:00:00+05:30", "end": "2099-01-02T20:00:00+05:30"}
    with pytest.raises(kernel.ToolFailed, match="end is not after"):
        agent_loop._normalized_event_times(bad, "add the thing")


def test_nearby_contextual_time_no_longer_hijacks_the_event(monkeypatch):
    """Round-6 P2: 'after the 7pm call, dinner at 8' — a stated time an
    hour away is context, not drift; 45-minute tolerance rejects it while
    real drift (minutes) still corrects. Shared implementation: the
    legacy path resolves to the same guards function."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    from kyraan.agents import guards

    args = {"start": "2099-01-02T20:00:00+05:30", "end": "2099-01-02T21:00:00+05:30"}
    start, end = guards.normalized_event_times(args, "after the 7pm call, add dinner")
    assert "T20:00:00" in start                     # 60 min away = context, kept

    drift = {"start": "2099-01-02T19:49:00+05:30", "end": "2099-01-02T20:49:00+05:30"}
    start, _ = guards.normalized_event_times(drift, "dinner at 8pm")
    assert "T20:00:00" in start                     # 11 min = drift, corrected

    # both brains share ONE implementation
    assert agent_loop._normalized_event_times is guards.normalized_event_times


def test_reference_point_times_are_grammar_filtered(monkeypatch):
    """Round-7 P2: 'after my 7:45pm call, add dinner at 8' — the decoy is
    15 minutes from the model's parse, inside any sane tolerance, but
    grammar marks it: reference-point prepositions exclude the match.
    'at 8pm' keeps anchoring."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    from kyraan.agents import guards

    args = {"start": "2099-01-02T20:00:00+05:30", "end": "2099-01-02T21:00:00+05:30"}
    start, end = guards.normalized_event_times(
        args, "after my 7:45pm call, add dinner at 8")
    assert "T20:00:00" in start and "T21:00:00" in end   # decoy filtered, model kept

    drift = {"start": "2099-01-02T19:49:00+05:30", "end": "2099-01-02T20:49:00+05:30"}
    start, _ = guards.normalized_event_times(drift, "add dinner at 8pm")
    assert "T20:00:00" in start                          # event-marker 'at' still anchors


def test_clause_scoped_and_lookahead_context_filtering(monkeypatch):
    """Round-8 P2 (final heuristic iteration, frozen hereafter): the
    reference word may sit earlier in the clause, or the decoy time may
    be followed by another event's noun — both are filtered; the plain
    event time still anchors."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    from kyraan.agents import guards

    args = {"start": "2099-01-02T20:00:00+05:30", "end": "2099-01-02T21:00:00+05:30"}
    for phrase in ("after my call at 7:45pm, add dinner at 8",
                   "once my 7:45pm call ends, add dinner",
                   "when the 7:45pm show finishes add dinner"):
        start, _ = guards.normalized_event_times(args, phrase)
        assert "T20:00:00" in start, phrase          # decoys filtered, model kept

    drift = {"start": "2099-01-02T19:49:00+05:30", "end": "2099-01-02T20:49:00+05:30"}
    start, _ = guards.normalized_event_times(drift, "add dinner at 8pm")
    assert "T20:00:00" in start                      # the real anchor still works


def test_window_vocabulary_never_filters_titles():
    """Round-8 P2: months/weekdays/ordinals/numerics are window words on
    the USER'S tokens — no dependence on the extractor echoing them."""
    from kyraan.agents import guards

    for w in ("feb", "February", "monday", "22nd", "2024", "3:30", "pm", "tonight"):
        assert guards.is_window_word(w), w
    for w in ("yoga", "dentist", "board", "rohan"):
        assert not guards.is_window_word(w), w


def test_round9_precision_fixes(monkeypatch):
    """Round-9: zero-noun end-verbs filter ('my call at 7:45pm ends');
    meals stay title-eligible; 'read' no longer hits 'unread'."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    from kyraan.agents import guards

    args = {"start": "2099-01-02T20:00:00+05:30", "end": "2099-01-02T21:00:00+05:30"}
    start, _ = guards.normalized_event_times(args, "My call at 7:45pm ends, add dinner at 8")
    assert "T20:00:00" in start

    assert not guards.is_window_word("dinner")
    assert not guards.is_window_word("lunch")
    assert guards.is_window_word("feb") and guards.is_window_word("tomorrow")

    assert not guards.wants_email_body("any unread emails?")
    assert not guards.wants_email_body("show my unread mail")
    assert guards.wants_email_body("open the first email")
    assert guards.wants_email_body("read the email from Rohan")
    assert guards.wants_email_body("summarize the latest email")


def test_round10_precision_fixes(monkeypatch):
    """Round-10: an end-verb followed by a time describes the RANGE and
    keeps the true start; duration words are window vocabulary; singular
    'say' triggers the body boundary."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    from kyraan.agents import guards

    # "dinner at 8pm and ends at 9pm" with drifted model values: pair
    # anchors to 8-9, never 9-10.
    drift = {"start": "2099-01-02T20:11:00+05:30", "end": "2099-01-02T21:11:00+05:30"}
    start, end = guards.normalized_event_times(drift, "dinner at 8pm and ends at 9pm")
    assert "T20:00:00" in start and "T21:00:00" in end

    # "my 7:45pm call ends" (no following time) still filters the decoy
    args = {"start": "2099-01-02T20:00:00+05:30", "end": "2099-01-02T21:00:00+05:30"}
    start, _ = guards.normalized_event_times(args, "my call at 7:45pm ends, add dinner at 8")
    assert "T20:00:00" in start

    for w in ("weeks", "days", "years", "two", "few"):
        assert guards.is_window_word(w), w
    assert not guards.is_window_word("second")   # positional, stays a filter

    assert guards.wants_email_body("what did the email say?")
    assert guards.wants_email_body("open the latest email")
    assert not guards.wants_email_body("any unread emails?")


async def test_empty_inbox_body_request_still_states_the_boundary(scripted_model, monkeypatch):
    """Round-10: 'read this email' with an empty inbox must lead with the
    metadata-only boundary, not imply opening is possible in principle."""
    async def fake_dispatch(spec, args):
        return {"unread_estimate": 0, "messages": []}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    monkeypatch.setattr(orchestrator, "_cloud_tier_in_use", lambda: True)
    scripted_model(['{"action": "call", "tool": "email.unread", "args": {}}'])

    reply = await agent_loop.run(90, "read this email please")
    assert "can't open email contents" in reply
    assert "no unread emails" in reply.lower()


async def test_delete_refuses_unlisted_ids_and_mismatched_titles(scripted_model, monkeypatch):
    """Security round P1: the confirmed title and the executed id must
    provably be the same event — unlisted ids are refused, and a
    mismatched title never deletes what the id points at."""
    dispatched = []

    async def fake_dispatch(spec, args):
        dispatched.append(args)
        return {"id": args.get("event_id"), "deleted": True, "already_gone": False}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)

    # unlisted id -> refused, model told to list first
    prompts = scripted_model([
        '{"action": "call", "tool": "calendar.delete_event", '
        '"args": {"event_id": "evX", "title": "Anything"}}',
        '{"action": "reply", "text": "Let me list your events first."}',
    ])
    await agent_loop.run(90, "delete it")
    assert dispatched == []
    assert "not from a CURRENT listing" in prompts[1]

    # listed id but WRONG title -> refused with the real title named
    import time as _time
    agent_loop._listing_cache[90] = {"at": _time.monotonic(), "items": {"ev1": "Board meeting"}}
    prompts = scripted_model([
        '{"action": "call", "tool": "calendar.delete_event", '
        '"args": {"event_id": "ev1", "title": "Yoga class"}}',
        '{"action": "reply", "text": "Those do not match — let me re-check."}',
    ])
    base = len(prompts)  # the fixture accumulates across installs
    await agent_loop.run(90, "delete the yoga class")
    assert dispatched == []
    assert "id/title mismatch" in prompts[base + 1] and "Board meeting" in prompts[base + 1]


async def test_stale_listing_and_missing_title_are_refused(scripted_model, monkeypatch):
    """Security round 2, P1: the binding is MANDATORY (no title, no
    delete) and bound to the CURRENT listing (a 10-minute-old one has
    expired)."""
    import time as _time
    dispatched = []

    async def fake_dispatch(spec, args):
        dispatched.append(args)
        return {"id": args.get("event_id"), "deleted": True, "already_gone": False}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)

    # stale listing -> refused
    agent_loop._listing_cache[90] = {"at": _time.monotonic() - 700,
                                     "items": {"ev1": "Board meeting"}}
    prompts = scripted_model([
        '{"action": "call", "tool": "calendar.delete_event", '
        '"args": {"event_id": "ev1", "title": "Board meeting"}}',
        '{"action": "reply", "text": "Let me refresh the listing."}',
    ])
    base = len(prompts)
    await agent_loop.run(90, "delete the board meeting")
    assert dispatched == []
    assert "CURRENT listing" in prompts[base + 1]

    # fresh listing but EMPTY title -> refused (binding is mandatory)
    agent_loop._listing_cache[90] = {"at": _time.monotonic(),
                                     "items": {"ev1": "Board meeting"}}
    prompts = scripted_model([
        '{"action": "call", "tool": "calendar.delete_event", '
        '"args": {"event_id": "ev1", "title": ""}}',
        '{"action": "reply", "text": "I need the exact title."}',
    ])
    base = len(prompts)
    await agent_loop.run(90, "delete it")
    assert dispatched == []
    assert "exactly as listed" in prompts[base + 1]


def test_pending_proposals_never_enter_cloud_prompts(monkeypatch, tmp_path):
    """Security round 3, P1: unapproved proposals ride only to LOCAL
    tiers — model-generated flags are not a trustworthy cloud boundary."""
    from kyraan.memory import store as mstore

    (mstore.PENDING_DIR).mkdir(parents=True, exist_ok=True)
    mstore.propose_fact("people/secret.md", "- A very private pending fact",
                        source="s", meta={"term": "long", "importance": "normal",
                                          "flags": [], "supersedes": None})

    # frontier (cloud) tier: placeholder only
    cloud_block = agent_loop._pending_block("frontier")
    assert "private pending fact" not in cloud_block
    assert "held locally" in cloud_block

    # cheap (local ollama) tier: the fact is visible
    local_block = agent_loop._pending_block("cheap")
    assert "A very private pending fact" in local_block


def test_home_tool_spec_carries_the_real_entity_roster():
    """Soak week day 1: the model guessed entity names, failed against
    the allowlist, then asked the OWNER for internal ids. The tool spec
    now carries the exact configured roster."""
    block = agent_loop._tools_block()
    assert "EXACTLY these" in block
    assert "switch.ac" in block


async def test_home_state_timestamps_are_humanized(monkeypatch):
    """A raw UTC ISO string leaked into a reply verbatim (and 5.5h off
    the owner's clock) — last_changed is humanized at the executor."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")

    async def fake_run_tool(call, **kwargs):
        return {"entity": "switch.ac", "state": "on",
                "last_changed": "2026-08-26T10:42:32.966246Z"}

    monkeypatch.setattr(agent_loop.kernel, "run_tool", fake_run_tool)
    result = await agent_loop._home_get_state(90, {"entity": "switch.ac"}, "is ac on")
    assert "Z" not in result["last_changed"]
    assert "4:12 PM" in result["last_changed"]


async def test_agent_creates_a_recurring_reminder(scripted_model, monkeypatch, tmp_path):
    from kyraan.triggers import scheduler as sched, store as rstore

    monkeypatch.setattr(rstore, "REMINDERS_PATH", tmp_path / "reminders.json")
    sched.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None,
               send_fn=None, only_chat=90)
    monkeypatch.setattr(kernel.config, "skill_config",
                        lambda name: {"permission": "auto", "model_tier": "cheap"})
    scripted_model([
        '{"action": "call", "tool": "reminders.create", '
        '"args": {"text": "take medicine", "when_iso": "2099-01-01T09:00:00+05:30", "repeat": "daily"}}',
        '{"action": "reply", "text": "Daily medicine reminder set for 9 AM."}',
    ])

    reply = await agent_loop.run(90, "remind me every day at 9am to take medicine")
    assert "Daily" in reply
    records = rstore.list_pending(90)
    assert len(records) == 1 and records[0].repeat == "daily"


async def test_deflection_reply_forces_one_re_decide(scripted_model, monkeypatch):
    """Live failure 2026-08-26 18:19: 'every evening at 8, check tomorrow's
    calendar...' answered with 'Do you want me to schedule it again?' —
    the doctrine bullet lost to the model imitating its own earlier bad
    replies in history. The guard is deterministic: a permission question
    gets exactly one forced re-decide with the error named."""
    called = []

    async def fake_dispatch(spec, args):
        called.append(spec.name)
        return {"state": "on"}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    prompts = scripted_model([
        '{"action": "reply", "text": "Sure. Do you want me to schedule it again: every day at 8 PM?"}',
        '{"action": "call", "tool": "home.get_state", "args": {"entity": "switch.ac"}}',
        '{"action": "reply", "text": "Done."}',
    ])

    reply = await agent_loop.run(90, "check the AC every evening")
    assert reply == "Done."
    assert called == ["home.get_state"]
    # the correction reached the model, quoting its own deflection
    assert "asked permission" in prompts[1]
    assert "Do you want me to schedule it again" in prompts[1]


async def test_third_deflection_stands_as_a_genuine_offer(scripted_model, monkeypatch):
    """A reply the model keeps standing by after being confronted is a
    proactive offer, not a deflection — the guard fires at most twice
    (live 2026-08-26: one draft swapped a pin-ask for a do-you-mean echo,
    both homework), then the answer stands; it never loops forever."""
    async def no_dispatch(spec, args):
        raise AssertionError("no tool should run")

    monkeypatch.setattr(reg, "dispatch", no_dispatch)
    offer = "You mentioned forgetting water — do you want me to set hourly reminders?"
    scripted_model([
        f'{{"action": "reply", "text": "{offer}"}}',
        f'{{"action": "reply", "text": "{offer}"}}',
        f'{{"action": "reply", "text": "{offer}"}}',
    ])

    reply = await agent_loop.run(90, "I keep forgetting to drink water these days")
    assert reply == offer


async def test_tool_executing_turn_skips_fact_extraction(scripted_model, monkeypatch):
    """Live failure 2026-08-26 18:20: the water-reminder command produced
    '📝 Noted for review: User wants reminders every hour...' — a command
    stored as a fact. Any turn that executed a tool must set the
    extraction skip, deterministically."""
    async def fake_dispatch(spec, args):
        return {"state": "on"}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    scripted_model([
        '{"action": "call", "tool": "home.get_state", "args": {"entity": "switch.ac"}}',
        '{"action": "reply", "text": "The AC is on."}',
    ])

    token = orchestrator._skip_extraction.set(False)
    try:
        await agent_loop.run(90, "is the AC on?")
        assert orchestrator._skip_extraction.get() is True
    finally:
        orchestrator._skip_extraction.reset(token)


async def test_plain_reply_turn_still_allows_extraction(scripted_model, monkeypatch):
    """The converse property: a statement turn (no tool ran) must NOT set
    the skip — that is where real facts come from."""
    async def no_dispatch(spec, args):
        raise AssertionError("no tool should run")

    monkeypatch.setattr(reg, "dispatch", no_dispatch)
    scripted_model(['{"action": "reply", "text": "Noted — congratulations!"}'])

    token = orchestrator._skip_extraction.set(False)
    try:
        await agent_loop.run(90, "my sister got a new job at the hospital")
        assert orchestrator._skip_extraction.get() is False
    finally:
        orchestrator._skip_extraction.reset(token)


async def test_empty_task_list_steers_to_reminders(scripted_model, monkeypatch):
    """Live 2026-08-26 18:30: 'task list' answered 'empty' while the water
    reminders existed — the owner says 'tasks' for both stores. An empty
    tasks.list result must carry the steer to check reminders.list."""
    async def no_dispatch(spec, args):
        raise AssertionError("tasks.list is store-backed, no adapter")

    monkeypatch.setattr(reg, "dispatch", no_dispatch)
    prompts = scripted_model([
        '{"action": "call", "tool": "tasks.list", "args": {}}',
        '{"action": "call", "tool": "reminders.list", "args": {}}',
        '{"action": "reply", "text": "No agent tasks, but your water reminder is active."}',
    ])

    reply = await agent_loop.run(90, "task list")
    assert "water reminder" in reply
    assert "call reminders.list NOW" in prompts[1]


async def test_homework_reply_counts_as_deflection(scripted_model, monkeypatch):
    """'If you want, I can list them — just say "list reminders"' is
    homework for something a tool answers right now; the guard fires."""
    async def no_dispatch(spec, args):
        raise AssertionError("no adapter call expected")

    monkeypatch.setattr(reg, "dispatch", no_dispatch)
    prompts = scripted_model([
        '{"action": "reply", "text": "If you want, I can list your reminders — just say \\"list reminders\\"."}',
        '{"action": "call", "tool": "reminders.list", "args": {}}',
        '{"action": "reply", "text": "You have 1 reminder: drink water hourly."}',
    ])

    reply = await agent_loop.run(90, "we have setup some tasks but you said empty task")
    assert "1 reminder" in reply
    assert "homework" in prompts[1]


async def test_menu_question_opening_counts_as_deflection(scripted_model, monkeypatch):
    """'task list' -> 'What would you like to do next—see your water
    reminders, or...' answered nothing (live 2026-08-26 18:40). A reply
    that OPENS with a menu question gets the forced re-decide; a real
    answer with a trailing question is untouched."""
    async def no_dispatch(spec, args):
        raise AssertionError("no adapter call expected")

    monkeypatch.setattr(reg, "dispatch", no_dispatch)
    prompts = scripted_model([
        '{"action": "reply", "text": "What would you like to do next—see your reminders, or cancel one?"}',
        '{"action": "call", "tool": "tasks.list", "args": {}}',
        '{"action": "reply", "text": "No scheduled tasks and no reminders."}',
    ])

    reply = await agent_loop.run(90, "task list")
    assert reply == "No scheduled tasks and no reminders."
    assert "homework" in prompts[1]
    # trailing question after real content does NOT match the guard
    assert agent_loop._DEFLECTION_RE.search(
        "Your task: 8 PM daily calendar check. What would you like to do next?") is None
    # a mid-reply offer after real content stands too — unanchored, this
    # pattern killed a good correction-acknowledgment and cornered the
    # model into a hallucinated non-sequitur (Amazon Pay, 2026-08-26)
    assert agent_loop._DEFLECTION_RE.search(
        "Got it — that's Avik, not Kiaan. If you want, I can re-enroll the face.") is None
    assert agent_loop._DEFLECTION_RE.search(
        "If you want, I can list them — just say 'list reminders'") is not None  # opener still caught


async def test_short_interval_asks_with_the_volume_math_then_creates(
        scripted_model, monkeypatch, tmp_path):
    """Owner's 2026-08-26 choice: sub-15-minute series are allowed but
    gated — the ask shows pings/day; the yes creates the exact series.
    5 minutes over 10:00-21:00 is the live request that set the policy."""
    from kyraan.triggers import scheduler as sch, store as rstore

    monkeypatch.setattr(rstore, "REMINDERS_PATH", tmp_path / "reminders.json")
    sch.init(schedule_fn=lambda *a, **k: None,
             cancel_fn=lambda *a, **k: None, send_fn=None)

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    scripted_model([
        '{"action": "call", "tool": "reminders.create", "args": {'
        '"text": "Drink water", "when_iso": "2099-01-01T10:00:00+05:30", '
        '"repeat": "interval", "interval_minutes": 5, '
        '"window_start": "10:00", "window_end": "21:00"}}',
    ])

    ask = await agent_loop.run(90, "remind me every 5 mins to drink water from 10am to 9pm")
    assert "every 5 minutes" in ask and "133 messages a day" in ask
    assert rstore.list_pending(90) == []  # nothing created before the yes

    reply = await orchestrator.handle_message(chat_id=90, raw_text="yes")
    records = rstore.list_pending(90)
    assert len(records) == 1 and records[0].interval_minutes == 5
    assert "Reminder set" in reply


async def test_replay_that_regates_reasks_instead_of_generic_error(monkeypatch):
    """A confirmed replay that re-raises ConfirmationRequired (a nested
    gate, or stale pre-fix code dropping the flag — seen live 2026-08-26
    on chat 90) must produce an honest re-ask, never fall to the
    catch-all's "Something went wrong" about an action that may have run."""
    import time as _time

    async def regating_handler(_args):
        raise kernel.ConfirmationRequired("reminders.create", {})

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    call = kernel.SkillCall("agent.action", {"tool": "reminders.create"})
    orchestrator._pending_confirmations[90] = (call, regating_handler, _time.monotonic())

    reply = await orchestrator.handle_message(chat_id=90, raw_text="yes")
    assert "Something went wrong" not in reply
    assert "confirmation" in reply and '"yes"' in reply
    assert 90 in orchestrator._pending_confirmations  # re-stashed, not lost


async def test_normal_interval_still_creates_without_a_confirm(
        scripted_model, monkeypatch, tmp_path):
    """>=15-minute series keep the instant path — no gate regression."""
    from kyraan.triggers import scheduler as sch, store as rstore

    monkeypatch.setattr(rstore, "REMINDERS_PATH", tmp_path / "reminders.json")
    sch.init(schedule_fn=lambda *a, **k: None,
             cancel_fn=lambda *a, **k: None, send_fn=None)
    scripted_model([
        '{"action": "call", "tool": "reminders.create", "args": {'
        '"text": "Drink water", "when_iso": "2099-01-01T10:00:00+05:30", '
        '"repeat": "interval", "interval_minutes": 60, '
        '"window_start": "10:00", "window_end": "21:00"}}',
        '{"action": "reply", "text": "Done — hourly water reminders are set."}',
    ])

    reply = await agent_loop.run(90, "remind me every hour to drink water")
    assert "Done" in reply
    assert len(rstore.list_pending(90)) == 1


async def test_interval_under_five_minutes_is_refused(
        scripted_model, monkeypatch, tmp_path):
    """The hard floor: below 5 minutes never even reaches a confirm —
    the model gets the refusal to relay."""
    from kyraan.triggers import scheduler as sch, store as rstore

    monkeypatch.setattr(rstore, "REMINDERS_PATH", tmp_path / "reminders.json")
    sch.init(schedule_fn=lambda *a, **k: None,
             cancel_fn=lambda *a, **k: None, send_fn=None)
    prompts = scripted_model([
        '{"action": "call", "tool": "reminders.create", "args": {'
        '"text": "check oven", "when_iso": "2099-01-01T10:00:00+05:30", '
        '"repeat": "interval", "interval_minutes": 2}}',
        '{"action": "reply", "text": "The smallest interval is 5 minutes — want that?"}',
    ])

    reply = await agent_loop.run(90, "remind me every 2 minutes to check the oven")
    assert "5 minutes" in reply
    assert rstore.list_pending(90) == []
    assert "smallest interval" in prompts[1]


async def test_cancel_returns_a_deterministic_receipt(scripted_model, monkeypatch, tmp_path):
    """After a successful cancel the model once replied with a menu
    ('Got it. What would you like to do next—...') instead of a receipt —
    past the deflection guard's opening anchor. Cancel outcomes are
    templated now, zero discretion (eval reminder.cancel, 2026-08-27)."""
    from kyraan.triggers import scheduler as sch, store as rstore

    monkeypatch.setattr(rstore, "REMINDERS_PATH", tmp_path / "reminders.json")
    sch.init(schedule_fn=lambda *a, **k: None,
             cancel_fn=lambda *a, **k: None, send_fn=None)
    r = sch.create_reminder(90, "Call mom", "2099-01-01T21:00:00+05:30")
    scripted_model([
        f'{{"action": "call", "tool": "reminders.cancel", "args": {{"reminder_id": "{r.id[:8]}"}}}}',
    ])

    reply = await agent_loop.run(90, "cancel my reminder")
    assert reply.startswith('Cancelled: "Call mom"')
    assert "won't fire again" in reply
    assert rstore.list_pending(90) == []


async def test_referent_dodge_forces_one_re_decide(scripted_model, monkeypatch):
    """Third live appearance of the pronoun disease (2026-08-27 23:41):
    "connect this doc with him" -> "who is 'him'?" although Kamal was
    the whole conversation. The prompt rule lost three times; the rail
    is deterministic — one person in the recent window means the
    pronoun is them."""
    from kyraan.agents import orchestrator
    chat_id = 91_001
    orchestrator._history[chat_id].append(
        ("user", "did you related it with kamal?"))
    orchestrator._history[chat_id].append(
        ("assistant", "Not automatically. There isn't a confirmation that "
                      "it's related to Kamal unless you tell me the "
                      "connection."))
    prompts = scripted_model([
        '{"action": "reply", "text": "Sure—who is \\"him\\" here (what exact person)?"}',
        '{"action": "reply", "text": "Linked the PDF to Kamal."}',
    ])
    reply = await agent_loop.run(chat_id, "connect this doc with him")
    assert reply == "Linked the PDF to Kamal."
    assert "exactly ONE person: Kamal" in prompts[1]


async def test_referent_question_stands_when_genuinely_ambiguous(
        scripted_model, monkeypatch):
    """Two people in the window = real ambiguity; the question stands."""
    from kyraan.agents import orchestrator
    chat_id = 91_002
    orchestrator._history[chat_id].append(
        ("user", "kamal and suman came over"))
    orchestrator._history[chat_id].append(
        ("assistant", "Nice — Kamal and Suman."))
    prompts = scripted_model([
        '{"action": "reply", "text": "Who do you mean by \\"him\\" — Kamal or Suman?"}',
    ])
    reply = await agent_loop.run(chat_id, "anything about him?")
    assert "Kamal or Suman" in reply
    assert len(prompts) == 1


def test_sole_recent_person_ignores_capitalized_noise():
    from kyraan.agents import orchestrator
    chat_id = 91_003
    orchestrator._history[chat_id].append(
        ("user", "did you know about kamal? yes you did"))
    orchestrator._history[chat_id].append(
        ("assistant", "Sure—Got it. When you sent it, Kamal was noted. "
                      "You can ask anytime."))
    assert agent_loop._sole_recent_person(chat_id, "connect it with him") \
        == "Kamal"


def test_new_dodge_shapes_are_caught():
    """Live 2026-08-28 00:57-00:59: scope interrogations and a
    which-NAME dodge slipped the rails."""
    assert agent_loop._DEFLECTION_RE.search(
        "Sure—what exactly do you want from your PDF?")
    assert agent_loop._DEFLECTION_RE.search(
        "Got it—do you want a summary of the entire PDF, or only parts?")
    assert agent_loop._REFERENT_DODGE_RE.search(
        "Which Kamal do you mean, and what happened?")
    # an answer that merely CONTAINS a question deep in it still passes
    assert not agent_loop._DEFLECTION_RE.search(
        "Here is the summary. Anything else?")


async def test_which_name_dodge_binds_to_the_named_person(
        scripted_model, monkeypatch):
    prompts = scripted_model([
        '{"action": "reply", "text": "Which Kamal do you mean, and what happened?"}',
        '{"action": "reply", "text": "From the saved PDF: Kamal recovered slowly."}',
    ])
    reply = await agent_loop.run(91_004, "What happened to Kamal's health?")
    assert reply == "From the saved PDF: Kamal recovered slowly."
    assert "exactly ONE person: Kamal" in prompts[1]


def test_identity_header_comes_from_the_registry_not_facts(monkeypatch):
    """2026-08-28: a poisoned, bulk-approved fact ("User goes by the
    name Ruma") made Kyraan call the owner by his wife's name. Identity
    is now a registry-derived header that SAYS it outranks facts."""
    from kyraan.control_plane import kernel as _kernel
    from kyraan.store import persons
    monkeypatch.setattr(persons, "name_map",
                        lambda: {"owner": "owner", "maan": "owner",
                                 "manab roy": "owner", "ruma": "ruma",
                                 "kamal": "kamal", "habu": "kamal"})
    block = agent_loop._identity_block(6755024720)
    assert "SPEAKER:" in block and "OWNER" in block
    assert "Ruma" in block and "NEVER the speaker" in block
    assert "OUTRANKS any saved fact" in block

    token = _kernel.set_viewer("ruma", "none")
    try:
        block = agent_loop._identity_block(8918323401)
        assert "NOT the owner" in block
        assert "person id ruma" in block
    finally:
        _kernel.reset_viewer_stage(token) if hasattr(
            _kernel, "reset_viewer_stage") else None


def test_persona_block_renders_from_config(monkeypatch):
    from kyraan.control_plane import config
    base = config.load()
    monkeypatch.setattr(config, "load", lambda: {
        **base, "persona": {"name": "Kyraan", "address_owner_as": "Maan",
                            "voice": ["Warm and direct."]}})
    block = agent_loop._persona_block()
    assert "You are Kyraan" in block
    assert "Address the owner as Maan" in block
    assert "Warm and direct." in block


# --- the reply contract (the concrete resolver, 2026-08-28) --------------

async def test_contract_ambiguous_referent_with_sole_person_is_challenged(
        scripted_model, monkeypatch):
    from kyraan.agents import orchestrator as _orch
    chat_id = 91_010
    _orch._history[chat_id].append(("user", "did you relate it with kamal?"))
    _orch._history[chat_id].append(("assistant", "Not yet — Kamal's PDF is saved."))
    prompts = scripted_model([
        '{"action": "reply", "answers_request": false, "reason": "ambiguous_referent", '
        '"text": "Who do you mean?"}',
        '{"action": "reply", "answers_request": true, "text": "Linked it to Kamal."}',
    ])
    reply = await agent_loop.run(chat_id, "connect this doc with him")
    assert reply == "Linked it to Kamal."
    assert "The referent is Kamal" in prompts[1]


async def test_contract_capability_claim_is_challenged_once(scripted_model):
    prompts = scripted_model([
        '{"action": "reply", "answers_request": false, "reason": "capability_missing", '
        '"text": "I cannot read email bodies."}',
        '{"action": "reply", "answers_request": false, "reason": "capability_missing", '
        '"text": "Truly no tool covers this."}',
    ])
    reply = await agent_loop.run(90, "make me a pizza")
    assert reply == "Truly no tool covers this."   # challenged, then stood by
    assert "Re-read the TOOLS list" in prompts[1]


async def test_contract_false_without_reason_is_rejected(scripted_model):
    prompts = scripted_model([
        '{"action": "reply", "answers_request": false, "text": "Hmm, what?"}',
        '{"action": "reply", "answers_request": true, "text": "Here is the answer."}',
    ])
    reply = await agent_loop.run(90, "what is 2+2")
    assert reply == "Here is the answer."
    assert "requires a valid reason" in prompts[1]


async def test_contract_missing_user_fact_question_stands(scripted_model):
    scripted_model([
        '{"action": "reply", "answers_request": false, "reason": "missing_user_fact", '
        '"text": "What time should the reminder be?"}',
    ])
    reply = await agent_loop.run(90, "remind me to call suman")
    assert "What time" in reply                    # the one legitimate question


async def test_contract_absent_field_passes_through(scripted_model):
    """Degraded-tier qwen may omit the field — treated as fulfilled; the
    regex rails stay as backstop."""
    scripted_model(['{"action": "reply", "text": "The AC is off."}'])
    reply = await agent_loop.run(90, "is the ac off?")
    assert reply == "The AC is off."
