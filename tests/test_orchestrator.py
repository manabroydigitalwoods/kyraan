"""Regression tests for orchestrator.handle_message's error handling.

Both scenarios here were found live: a malformed reminder extraction (the
model produced a duplicated UTC offset, "+05:30+04:00") raised an uncaught
ValueError that crashed the whole call — kernel.run_skill re-raises after
logging, and handle_message only caught three specific exception types, so
anything else propagated all the way out. In the TUI this broke the app's
ability to handle further input, since the exception crossed an
asyncio.to_thread boundary uncaught.
"""
from dataclasses import dataclass

import pytest

from kyraan.agents import orchestrator
from kyraan.intent.normalize import NormalizedIntent


@dataclass
class _FakeRouted:
    text: str


def _mock_normalize(monkeypatch, intent: str, normalized_text: str = "") -> None:
    def fake_normalize(raw_text, tier="cheap", history=""):
        return NormalizedIntent(intent=intent, confidence=1.0, normalized_text=normalized_text or raw_text)

    monkeypatch.setattr(orchestrator, "normalize", fake_normalize)


async def test_unexpected_handler_exception_does_not_crash(monkeypatch):
    """A generic, unanticipated exception from inside a skill handler must
    be caught by handle_message's final safety net, not propagate."""
    _mock_normalize(monkeypatch, "qa.answer")

    def raise_unexpected(**kwargs):
        raise RuntimeError("something the code never anticipated")

    monkeypatch.setattr(orchestrator.router, "call", raise_unexpected)

    result = await orchestrator.handle_message(chat_id=0, raw_text="hello")
    assert "went wrong" in result.lower()


async def test_malformed_reminder_json_gives_a_clear_message_not_a_crash(monkeypatch):
    _mock_normalize(monkeypatch, "reminders.create", "remind me to test")

    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="not valid json"))

    result = await orchestrator.handle_message(chat_id=0, raw_text="remind me to test")
    assert "couldn't work out a time" in result.lower()


async def test_reminder_with_unparseable_datetime_gives_a_clear_message(monkeypatch):
    """The exact live failure: valid JSON, but when_iso has a duplicated
    UTC offset ("+05:30+04:00") that datetime.fromisoformat() rejects. This
    only surfaces once scheduler.init() has wired up _schedule_fn (as the
    real app does on mount) — without it, create_reminder() hits an
    unrelated AssertionError first, which would mask the bug this test
    exists to guard against."""
    _mock_normalize(monkeypatch, "reminders.create", "remind me to test")

    monkeypatch.setattr(
        orchestrator.router,
        "call",
        lambda **kwargs: _FakeRouted(text='{"text": "test", "when_iso": "2026-13-45Tnot-a-time"}'),
    )
    orchestrator.scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=None)

    result = await orchestrator.handle_message(chat_id=0, raw_text="remind me to test")
    assert "couldn't work out a time" in result.lower()


async def test_well_formed_reminder_still_works(monkeypatch):
    """Guard against the error-handling addition accidentally swallowing
    the success path too."""
    _mock_normalize(monkeypatch, "reminders.create", "remind me to test")

    monkeypatch.setattr(
        orchestrator.router,
        "call",
        lambda **kwargs: _FakeRouted(text='{"text": "test", "when_iso": "2026-08-25T13:26:44+05:30"}'),
    )

    class _FakeReminder:
        id = "abcdef1234567890"

    monkeypatch.setattr(orchestrator.scheduler, "create_reminder", lambda *a, **k: _FakeReminder())

    result = await orchestrator.handle_message(chat_id=0, raw_text="remind me to test")
    assert "Reminder set" in result
    assert "couldn't work out a time" not in result.lower()


async def test_qa_system_prompt_forbids_claiming_a_fact_is_already_saved(monkeypatch):
    """Found live (2026-08-25): "remember that my wife's name is Mira" got
    "Got it—I've noted that" back while nothing was written. With the
    memory loop wired, facts go live only after human review — so the
    prompt must still forbid claiming a fact is already permanently
    saved."""
    _mock_normalize(monkeypatch, "qa.answer")
    captured = {}

    def fake_call(prompt, system="", **kwargs):
        captured["system"] = system
        return _FakeRouted(text="Noted — it'll be saved after review.")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)

    await orchestrator.handle_message(chat_id=0, raw_text="remember that my wife's name is Mira")
    assert "already permanently" in captured["system"]


@pytest.fixture(autouse=True)
def _clear_module_state():
    """_pending_confirmations and _history are module-level state — never
    let one test's leftovers leak into another."""
    orchestrator._pending_confirmations.clear()
    orchestrator._history.clear()
    yield
    orchestrator._pending_confirmations.clear()
    orchestrator._history.clear()


@pytest.fixture(autouse=True)
def _no_real_extraction(monkeypatch):
    """handle_message now runs fact extraction after every dispatch — a
    real model call. Neutralize it by default so every pre-existing test
    stays hermetic; extraction-specific tests override this seam."""

    async def fake_propose(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", fake_propose)


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator.scheduler.store, "REMINDERS_PATH", tmp_path / "reminders.json")
    yield


async def test_cancel_with_multiple_pending_and_no_id_asks_instead_of_guessing(monkeypatch, isolated_store):
    """The live walkthrough's cancel-by-description only passed because the
    intended reminder happened to be first in the list — the old fallback
    cancelled pending[0] whenever no id matched. With several reminders
    that's a silent wrong deletion; it must ask instead."""
    _mock_normalize(monkeypatch, "reminders.cancel", "cancel my reminder")
    store = orchestrator.scheduler.store
    store.add(chat_id=0, text="call mom", when_iso="2099-01-01T10:00:00+00:00")
    store.add(chat_id=0, text="call ruma", when_iso="2099-01-01T11:00:00+00:00")

    result = await orchestrator.handle_message(chat_id=0, raw_text="cancel my reminder")

    assert "which should I cancel" in result
    assert "call mom" in result and "call ruma" in result
    assert len(store.list_pending(0)) == 2  # nothing was deleted


async def test_cancel_with_single_pending_and_no_id_cancels_it(monkeypatch, isolated_store):
    _mock_normalize(monkeypatch, "reminders.cancel", "cancel my reminder")
    store = orchestrator.scheduler.store
    store.add(chat_id=0, text="call mom", when_iso="2099-01-01T10:00:00+00:00")

    result = await orchestrator.handle_message(chat_id=0, raw_text="cancel my reminder")

    assert 'Cancelled reminder: "call mom"' in result
    assert store.list_pending(0) == []


async def test_cancel_by_id_picks_the_matching_reminder_not_the_first(monkeypatch, isolated_store):
    store = orchestrator.scheduler.store
    store.add(chat_id=0, text="call mom", when_iso="2099-01-01T10:00:00+00:00")
    second = store.add(chat_id=0, text="call ruma", when_iso="2099-01-01T11:00:00+00:00")
    _mock_normalize(monkeypatch, "reminders.cancel", f"cancel {second.id[:8]}")

    result = await orchestrator.handle_message(chat_id=0, raw_text=f"cancel {second.id[:8]}")

    assert 'Cancelled reminder: "call ruma"' in result
    assert [r.text for r in store.list_pending(0)] == ["call mom"]


def _make_skill_confirm(monkeypatch, skill_name: str) -> None:
    """Make one skill require confirm-first, everything else auto."""
    from kyraan.control_plane import kernel

    def fake_skill_config(name):
        return {"permission": "confirm" if name == skill_name else "auto", "model_tier": "cheap"}

    monkeypatch.setattr(kernel.config, "skill_config", fake_skill_config)


async def test_confirm_flow_asks_then_runs_on_yes(monkeypatch, isolated_store):
    """The kernel could always raise ConfirmationRequired, but there was no
    path for the user to actually say yes — the orchestrator's old handler
    just replied 'this shouldn't happen'. Now: ask, stash, run on 'yes'."""
    _make_skill_confirm(monkeypatch, "reminders.create")
    _mock_normalize(monkeypatch, "reminders.create", "remind me to test")
    monkeypatch.setattr(
        orchestrator.router,
        "call",
        lambda **kwargs: _FakeRouted(text='{"text": "test", "when_iso": "2099-01-01T10:00:00+00:00"}'),
    )
    orchestrator.scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=None)

    ask = await orchestrator.handle_message(chat_id=0, raw_text="remind me to test")
    assert "needs your confirmation" in ask
    assert orchestrator.scheduler.store.list_pending(0) == []  # not run yet

    result = await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert "Reminder set" in result
    assert len(orchestrator.scheduler.store.list_pending(0)) == 1


async def test_confirm_flow_no_cancels_without_running(monkeypatch, isolated_store):
    _make_skill_confirm(monkeypatch, "reminders.create")
    _mock_normalize(monkeypatch, "reminders.create", "remind me to test")

    ask = await orchestrator.handle_message(chat_id=0, raw_text="remind me to test")
    assert "needs your confirmation" in ask

    result = await orchestrator.handle_message(chat_id=0, raw_text="no")
    assert "cancelled" in result.lower()
    assert orchestrator.scheduler.store.list_pending(0) == []
    assert orchestrator._pending_confirmations == {}


async def test_confirm_flow_unrelated_message_drops_the_pending_action(monkeypatch, isolated_store):
    """A different message while a confirmation is pending must fail safe:
    the stashed action is dropped (never run implicitly) and the new
    message is handled normally."""
    _make_skill_confirm(monkeypatch, "reminders.create")
    _mock_normalize(monkeypatch, "reminders.create", "remind me to test")
    await orchestrator.handle_message(chat_id=0, raw_text="remind me to test")

    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="It's 2pm."))

    result = await orchestrator.handle_message(chat_id=0, raw_text="what time is it?")
    assert result == "It's 2pm."
    assert orchestrator._pending_confirmations == {}
    assert orchestrator.scheduler.store.list_pending(0) == []  # the reminder never ran


# --- Memory loop: extraction note, conversation history, facts in prompt ---


async def test_reply_gets_a_noted_for_review_line_when_a_fact_is_queued(monkeypatch):
    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="Nice!"))

    async def fake_propose(raw_text, context="", insist=False):
        return ["- Wife's name is Mira"]

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", fake_propose)

    result = await orchestrator.handle_message(chat_id=0, raw_text="my wife's name is Mira")
    assert result.startswith("Nice!")
    assert "Noted for review: Wife's name is Mira" in result


async def test_extraction_failure_never_breaks_the_reply(monkeypatch):
    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="Nice!"))

    async def broken_propose(raw_text, context=""):
        raise RuntimeError("extraction blew up")

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", broken_propose)

    result = await orchestrator.handle_message(chat_id=0, raw_text="my wife's name is Mira")
    assert result == "Nice!"


async def test_short_messages_skip_extraction_entirely(monkeypatch):
    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="Hey!"))
    calls = []

    async def counting_propose(raw_text, context=""):
        calls.append(raw_text)
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", counting_propose)

    await orchestrator.handle_message(chat_id=0, raw_text="hi")
    assert calls == []


async def test_qa_prompt_carries_conversation_history_and_facts(monkeypatch):
    """The second message's system prompt must contain the first exchange
    (rolling history) and the live memory facts."""
    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.memory_store, "load_all_facts", lambda **kwargs: "FACTS_SENTINEL")
    systems = []

    def fake_call(prompt, system="", **kwargs):
        systems.append(system)
        return _FakeRouted(text="Blue, you told me.")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)

    await orchestrator.handle_message(chat_id=0, raw_text="my favourite colour is blue")
    await orchestrator.handle_message(chat_id=0, raw_text="what's my favourite colour?")

    assert "FACTS_SENTINEL" in systems[-1]
    assert "user: my favourite colour is blue" in systems[-1]
    assert "assistant: Blue, you told me." in systems[-1]
    # And the first call must NOT have seen history that didn't exist yet
    assert "user: my favourite colour is blue" not in systems[0]


async def test_history_is_per_chat_and_rolls_over(monkeypatch):
    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="ok then"))

    for i in range(25):  # 25 exchanges = 50 entries > the 40-entry window
        await orchestrator.handle_message(chat_id=1, raw_text=f"message number {i}")

    block = orchestrator._history_block(1)
    assert "message number 0 " not in block + " "  # rolled out
    assert "message number 24" in block
    assert orchestrator._history_block(2) == "(no conversation yet)"  # other chats unaffected


async def test_intent_classification_falls_back_to_cheap_when_frontier_is_down(monkeypatch):
    """Classification single-points on Groq; a provider outage must degrade
    to the local cheap tier, not refuse to understand anything."""
    tiers_called = []

    def fake_normalize(raw_text, tier="cheap", history=""):
        tiers_called.append(tier)
        if tier == "frontier":
            raise orchestrator.router.ModelProviderError("groq is down")
        return NormalizedIntent(intent="qa.answer", confidence=1.0, normalized_text=raw_text)

    monkeypatch.setattr(orchestrator, "normalize", fake_normalize)
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="hi!"))

    result = await orchestrator.handle_message(chat_id=0, raw_text="hello there")
    assert result == "hi!"
    assert tiers_called == ["frontier", "cheap"]


async def test_budget_alert_note_appended_once(monkeypatch):
    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="ok"))
    monkeypatch.setattr(orchestrator.router, "budget_alert_due", lambda: True)
    monkeypatch.setattr(orchestrator.router, "today_cost_usd", lambda: 4.20)
    monkeypatch.setattr(orchestrator.router, "budget_alert_threshold_pct", lambda: 80.0)
    monkeypatch.setattr(orchestrator.router, "daily_budget_usd", lambda: 5.00)

    result = await orchestrator.handle_message(chat_id=0, raw_text="hello there")
    assert "⚠️" in result and "$4.20" in result and "$5.00" in result


async def test_calendar_intent_formats_events_from_the_tool(monkeypatch):
    _mock_normalize(monkeypatch, "calendar.list", "what's on my calendar today")
    monkeypatch.setattr(
        orchestrator.router,
        "call",
        lambda **kwargs: _FakeRouted(
            text='{"start_iso": "2026-08-25T00:00:00+05:30", "end_iso": "2026-08-25T23:59:59+05:30", "label": "today"}'
        ),
    )

    async def fake_run_tool(call, **kwargs):
        assert call.tool_name == "calendar.list_events"
        assert call.args == {"start": "2026-08-25T00:00:00+05:30", "end": "2026-08-25T23:59:59+05:30"}
        return [
            {"title": "Standup", "start": "2026-08-25T09:30:00+00:00", "end": "2026-08-25T09:45:00+00:00", "all_day": False, "location": None},
            {"title": "Holiday", "start": "2026-08-25T00:00:00+00:00", "end": "2026-08-26T00:00:00+00:00", "all_day": True, "location": None},
        ]

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)

    result = await orchestrator.handle_message(chat_id=0, raw_text="what's on my calendar today")
    assert "Calendar today:" in result
    assert "9:30 AM — Standup" in result
    assert "all day — Holiday" in result


async def test_calendar_tool_failure_surfaces_honestly(monkeypatch):
    _mock_normalize(monkeypatch, "calendar.list", "what's on my calendar today")
    monkeypatch.setattr(
        orchestrator.router,
        "call",
        lambda **kwargs: _FakeRouted(
            text='{"start_iso": "2026-08-25T00:00:00+05:30", "end_iso": "2026-08-25T23:59:59+05:30", "label": "today"}'
        ),
    )

    async def failing_run_tool(call, **kwargs):
        raise orchestrator.kernel.ToolFailed("calendar.list_events failed: GOOGLE_CALENDAR_ICS_URL is not set")

    monkeypatch.setattr(orchestrator.kernel, "run_tool", failing_run_tool)

    result = await orchestrator.handle_message(chat_id=0, raw_text="what's on my calendar today")
    assert "Couldn't check the calendar" in result
    assert "GOOGLE_CALENDAR_ICS_URL" in result


async def test_classifier_receives_conversation_context(monkeypatch):
    """The live bug this guards: follow-ups ("go ahead", "the call mom
    one", "6pm") were classified with no context, so Kyraan lost the
    thread of what the user was continuing."""
    captured = {}

    def fake_normalize(raw_text, tier="cheap", history=""):
        captured["history"] = history
        return NormalizedIntent(intent="qa.answer", confidence=1.0, normalized_text=raw_text)

    monkeypatch.setattr(orchestrator, "normalize", fake_normalize)
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="ok"))

    await orchestrator.handle_message(chat_id=3, raw_text="remind me to call the plumber tomorrow")
    await orchestrator.handle_message(chat_id=3, raw_text="go ahead")

    assert "remind me to call the plumber tomorrow" in captured["history"]


async def test_classifier_context_is_clipped(monkeypatch):
    orchestrator._history[4].append(("assistant", "x" * 5000))
    context = orchestrator._classifier_context(4)
    assert len(context) < 300 and context.endswith("…")


async def test_cancel_by_unique_description_cancels_the_right_one(monkeypatch, isolated_store):
    _mock_normalize(monkeypatch, "reminders.cancel", "cancel the call mom reminder")
    store = orchestrator.scheduler.store
    store.add(chat_id=0, text="call mom", when_iso="2099-01-01T10:00:00+00:00")
    store.add(chat_id=0, text="water the plants", when_iso="2099-01-01T11:00:00+00:00")

    result = await orchestrator.handle_message(chat_id=0, raw_text="cancel the call mom reminder")

    assert 'Cancelled reminder: "call mom"' in result
    assert [r.text for r in store.list_pending(0)] == ["water the plants"]


async def test_cancel_with_description_matching_several_still_asks(monkeypatch, isolated_store):
    _mock_normalize(monkeypatch, "reminders.cancel", "cancel the call reminder")
    store = orchestrator.scheduler.store
    store.add(chat_id=0, text="call mom", when_iso="2099-01-01T10:00:00+00:00")
    store.add(chat_id=0, text="call the plumber", when_iso="2099-01-01T11:00:00+00:00")

    result = await orchestrator.handle_message(chat_id=0, raw_text="cancel the call reminder")

    assert "which should I cancel" in result
    assert len(store.list_pending(0)) == 2


async def test_qa_prompt_forbids_conflating_reminders_and_calendar_events(monkeypatch):
    """Found live (pre-writes): asked to add a calendar event, qa.answer
    played along and a reminder got created that the user believed was a
    calendar event. Writes exist now, but the honesty guards stay: never
    claim an event was created unless it was, never present a reminder as
    a calendar event."""
    _mock_normalize(monkeypatch, "qa.answer")
    captured = {}

    def fake_call(prompt, system="", **kwargs):
        captured["system"] = system
        return _FakeRouted(text="ok")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)

    await orchestrator.handle_message(chat_id=0, raw_text="can you set an event in my calendar")
    assert "unless it actually did" in captured["system"]
    assert "is not a calendar" in captured["system"]


async def test_duplicate_reminder_is_refused_with_the_existing_id(monkeypatch, isolated_store):
    """Found live: asking again after a reminder was already set created a
    second identical one — two pings for one intent."""
    _mock_normalize(monkeypatch, "reminders.create", "remind me to call suman at 7pm")
    monkeypatch.setattr(
        orchestrator.router,
        "call",
        lambda **kwargs: _FakeRouted(text='{"text": "call Suman", "when_iso": "2099-01-01T19:00:00+05:30"}'),
    )
    orchestrator.scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=None)

    first = await orchestrator.handle_message(chat_id=0, raw_text="remind me to call suman at 7pm")
    assert "Reminder set" in first

    second = await orchestrator.handle_message(chat_id=0, raw_text="set a reminder to call suman at 7pm")
    assert "Already set" in second and "didn't add a duplicate" in second
    assert len(orchestrator.scheduler.store.list_pending(0)) == 1


async def test_same_text_at_a_different_time_is_not_a_duplicate(monkeypatch, isolated_store):
    _mock_normalize(monkeypatch, "reminders.create", "remind me to call suman")
    responses = iter([
        _FakeRouted(text='{"text": "call Suman", "when_iso": "2099-01-01T19:00:00+05:30"}'),
        _FakeRouted(text='{"text": "call Suman", "when_iso": "2099-01-01T21:00:00+05:30"}'),
    ])
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: next(responses))
    orchestrator.scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=None)

    await orchestrator.handle_message(chat_id=0, raw_text="remind me to call suman at 7pm")
    second = await orchestrator.handle_message(chat_id=0, raw_text="remind me to call suman at 9pm too")
    assert "Reminder set" in second
    assert len(orchestrator.scheduler.store.list_pending(0)) == 2


async def test_calendar_create_asks_then_creates_the_exact_confirmed_event(monkeypatch):
    """The full confirm chain, unmocked at the kernel: the confirm-gated
    write tool halts the auto skill, the ask names the concrete event, and
    "yes" runs it with byte-identical args (extraction is NOT re-run)."""
    from kyraan.tools import registry as reg

    _mock_normalize(monkeypatch, "calendar.create", "add call suman tomorrow 5pm to my calendar")
    extraction_calls = []

    def fake_call(prompt, system="", **kwargs):
        extraction_calls.append(prompt)
        return _FakeRouted(
            text='{"title": "Call Suman", "start_iso": "2099-01-02T17:00:00+00:00", "end_iso": "2099-01-02T18:00:00+00:00", "location": null}'
        )

    monkeypatch.setattr(orchestrator.router, "call", fake_call)
    dispatched = []

    async def fake_dispatch(spec, args):
        dispatched.append((spec.name, args))
        return {"id": "ev1", "link": "https://cal/ev1", "title": args["title"]}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)

    ask = await orchestrator.handle_message(chat_id=0, raw_text="add call suman tomorrow 5pm to my calendar")
    assert "About to create a calendar event" in ask and "Call Suman" in ask and "5:00 PM" in ask
    assert dispatched == []  # nothing written before the yes

    result = await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert "Event created" in result and "https://cal/ev1" in result
    assert dispatched == [("calendar.create_event", {
        "title": "Call Suman", "start": "2099-01-02T17:00:00+00:00", "end": "2099-01-02T18:00:00+00:00"})]
    assert len(extraction_calls) == 1  # confirm did NOT re-extract


async def test_calendar_create_no_cancels_without_writing(monkeypatch):
    from kyraan.tools import registry as reg

    _mock_normalize(monkeypatch, "calendar.create", "add call suman tomorrow 5pm to my calendar")
    monkeypatch.setattr(
        orchestrator.router, "call",
        lambda **kwargs: _FakeRouted(
            text='{"title": "Call Suman", "start_iso": "2099-01-02T17:00:00+00:00", "end_iso": "2099-01-02T18:00:00+00:00", "location": null}'
        ),
    )
    dispatched = []

    async def fake_dispatch(spec, args):
        dispatched.append(1)

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)

    await orchestrator.handle_message(chat_id=0, raw_text="add call suman tomorrow 5pm to my calendar")
    result = await orchestrator.handle_message(chat_id=0, raw_text="no")
    assert "cancelled" in result.lower()
    assert dispatched == []


async def test_event_extraction_junk_is_cleaned_before_the_ask(monkeypatch):
    """Seen live in the first real confirmation ask: microsecond noise
    (15:00:00.000123) and the string "null" as a location ('at null').
    Both must be scrubbed before the user sees or confirms anything."""
    from kyraan.tools import registry as reg

    _mock_normalize(monkeypatch, "calendar.create", "add test event tomorrow 3pm to my calendar")
    monkeypatch.setattr(
        orchestrator.router, "call",
        lambda **kwargs: _FakeRouted(
            text='{"title": "Test Event", "start_iso": "2099-01-02T15:00:00.000123+00:00", "end_iso": "2099-01-02T16:00:00.000124+00:00", "location": "null"}'
        ),
    )
    dispatched = []

    async def fake_dispatch(spec, args):
        dispatched.append(args)
        return {"id": "ev1", "link": None, "title": args["title"]}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)

    ask = await orchestrator.handle_message(chat_id=0, raw_text="add test event tomorrow 3pm to my calendar")
    assert ".000123" not in ask and "at null" not in ask
    assert "3:00 PM" in ask and "T15:00:00.000123" not in ask

    await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert dispatched == [{"title": "Test Event", "start": "2099-01-02T15:00:00+00:00", "end": "2099-01-02T16:00:00+00:00"}]


async def test_home_query_reports_state_and_power(monkeypatch):
    _mock_normalize(monkeypatch, "home.query")
    from datetime import timedelta
    from kyraan.control_plane.dnd import local_now
    two_h_ago = (local_now() - timedelta(hours=2, minutes=5)).isoformat()
    readings = {
        "switch.ac": {"entity": "switch.ac", "state": "on", "unit": None, "name": "AC", "last_changed": two_h_ago},
        "sensor.ac_current_consumption": {"entity": "sensor.ac_current_consumption", "state": "359.5", "unit": "W", "name": "AC power"},
        "sensor.ac_today_s_consumption": {"entity": "sensor.ac_today_s_consumption", "state": "2.098", "unit": "kWh", "name": "AC today"},
        "sensor.bed_room_temp_temperature": {"entity": "sensor.bed_room_temp_temperature", "state": "27.4", "unit": "\u00b0C", "name": "Temp"},
        "sensor.bed_room_temp_humidity": {"entity": "sensor.bed_room_temp_humidity", "state": "83", "unit": "%", "name": "Humidity"},
    }

    async def fake_run_tool(call, **kwargs):
        return readings[call.args["entity"]]

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)
    result = await orchestrator.handle_message(chat_id=0, raw_text="is the AC on?")
    assert "The AC is ON for 2h 05m" in result and "359.5 W" in result and "2.098 kWh" in result
    assert "Bedroom" not in result  # AC question gets the AC answer only


async def test_home_control_asks_then_switches_with_readback(monkeypatch):
    """The confirm chain unmocked at the kernel: OFF is parsed
    deterministically, nothing switches before the yes, and the reply
    reports the plug's read-back state."""
    from kyraan.tools import registry as reg

    _mock_normalize(monkeypatch, "home.control", "turn off the AC")
    dispatched = []

    async def fake_dispatch(spec, args):
        dispatched.append(spec.name)
        return {"entity": "switch.ac", "state": "off", "unit": None, "name": "AC"}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)

    ask = await orchestrator.handle_message(chat_id=0, raw_text="turn off the AC")
    assert "About to turn the AC OFF" in ask
    assert dispatched == []

    result = await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert "the AC is now OFF" in result
    assert dispatched == ["home.turn_off"]


async def test_home_control_without_direction_asks(monkeypatch):
    _mock_normalize(monkeypatch, "home.control", "do something with the AC")
    result = await orchestrator.handle_message(chat_id=0, raw_text="do something with the AC")
    assert "on or off" in result


def _home_readings():
    return {
        "switch.ac": {"entity": "switch.ac", "state": "on", "unit": None, "name": "AC", "last_changed": None},
        "sensor.ac_current_consumption": {"entity": "sensor.ac_current_consumption", "state": "350", "unit": "W", "name": "p"},
        "sensor.ac_today_s_consumption": {"entity": "sensor.ac_today_s_consumption", "state": "2.1", "unit": "kWh", "name": "t"},
        "sensor.bed_room_temp_temperature": {"entity": "sensor.bed_room_temp_temperature", "state": "27.4", "unit": "°C", "name": "T"},
        "sensor.bed_room_temp_humidity": {"entity": "sensor.bed_room_temp_humidity", "state": "83", "unit": "%", "name": "H"},
    }


async def test_temperature_question_gets_climate_only(monkeypatch):
    _mock_normalize(monkeypatch, "home.query", "what is the bedroom temperature?")
    readings = _home_readings()

    async def fake_run_tool(call, **kwargs):
        return readings[call.args["entity"]]

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)
    result = await orchestrator.handle_message(chat_id=0, raw_text="what is the bedroom temperature?")
    assert "Bedroom: 27.4°C / 83% humidity." in result
    assert "The AC" not in result


async def test_unsensored_room_gets_an_honest_no_sensor_answer(monkeypatch):
    """Seen live: "kitchen room temp?" answered with the bedroom card as if
    it were the kitchen. It must say there's no sensor there."""
    _mock_normalize(monkeypatch, "home.query", "kitchen room temp?")
    readings = _home_readings()

    async def fake_run_tool(call, **kwargs):
        return readings[call.args["entity"]]

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)
    result = await orchestrator.handle_message(chat_id=0, raw_text="kitchen room temp?")
    assert "no sensor in the kitchen room" in result
    assert "Bedroom: 27.4°C" in result  # still offers the reading it has
    assert "The AC" not in result


async def test_email_check_redacts_history_only_when_a_cloud_tier_is_active(monkeypatch):
    """The §3a data boundary: with any CLOUD model tier, history records
    only a placeholder so subjects never reach third parties. With
    local-only tiers (2026-08-26) redaction is pure capability loss — qa
    couldn't see the listing a follow-up was asking about — so the real
    text stays."""
    _mock_normalize(monkeypatch, "email.check")

    async def fake_run_tool(call, **kwargs):
        assert call.tool_name == "email.unread"
        return {"unread_estimate": 3, "messages": [
            {"from": '"Suman Das" <s@x.com>', "subject": "Invoice pending", "date": "d"},
            {"from": "noreply@bank.com", "subject": "Statement", "date": "d"},
        ]}

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)

    monkeypatch.setattr(orchestrator, "_cloud_tier_in_use", lambda: True)
    result = await orchestrator.handle_message(chat_id=0, raw_text="any new emails?")
    assert "about 3 unread" in result
    assert "Suman Das: Invoice pending" in result
    assert "noreply@bank.com: Statement" in result
    history = orchestrator._history_block(0)
    assert "Invoice pending" not in history and "Suman Das" not in history
    assert "[showed the unread email summary]" in history

    monkeypatch.setattr(orchestrator, "_cloud_tier_in_use", lambda: False)
    await orchestrator.handle_message(chat_id=1, raw_text="any new emails?")
    assert "Invoice pending" in orchestrator._history_block(1)  # local-only: qa can see it


async def test_email_failure_surfaces_and_history_stays_clean(monkeypatch):
    _mock_normalize(monkeypatch, "email.check")

    async def failing_run_tool(call, **kwargs):
        raise orchestrator.kernel.ToolFailed("email.unread failed: re-run setup_google_oauth")

    monkeypatch.setattr(orchestrator.kernel, "run_tool", failing_run_tool)
    result = await orchestrator.handle_message(chat_id=0, raw_text="any new emails?")
    assert "Couldn't check email" in result


async def test_unconverged_switch_reply_is_honest(monkeypatch):
    _mock_normalize(monkeypatch, "home.control", "turn on the AC")

    calls = {"n": 0}

    async def fake_run_tool(call, **kwargs):
        # Production gating: the first (unconfirmed) attempt raises; the
        # confirmed re-run executes and reports unconverged.
        calls["n"] += 1
        if calls["n"] == 1:
            raise orchestrator.ConfirmationRequired(call.tool_name, call.args)
        return {"entity": "switch.ac", "state": "off", "converged": False, "unit": None, "name": "AC"}

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)
    await orchestrator.handle_message(chat_id=0, raw_text="turn on the AC")
    result = await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert "still reports OFF" in result and "Done" not in result


async def test_proactive_sends_enter_history(monkeypatch):
    """Seen live: 'Thanks for the reminder' got 'I didn't actually send
    you any reminders' — fire() bypassed history entirely."""
    orchestrator.record_proactive(0, "Reminder: call Suman")
    assert "assistant: Reminder: call Suman" in orchestrator._history_block(0)


async def test_open_email_request_states_the_body_boundary(monkeypatch):
    """Seen live: "can you open email?" got the same unread list as if it
    answered the question — it must state the metadata-only boundary."""
    _mock_normalize(monkeypatch, "email.check", "can you open the email?")

    async def fake_run_tool(call, **kwargs):
        return {"unread_estimate": 1, "messages": [{"from": "a@b.c", "subject": "Hi", "date": "d"}]}

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)
    result = await orchestrator.handle_message(chat_id=0, raw_text="can you open the email?")
    assert "can't open email contents" in result and "senders and" in result


async def test_chat_transcript_logs_both_sides(monkeypatch):
    from kyraan.control_plane import logging_setup

    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="hello back"))
    await orchestrator.handle_message(chat_id=5, raw_text="hello there")

    log = logging_setup.CHAT_LOG.read_text()
    assert '"role": "user", "text": "hello there"' in log
    assert '"role": "assistant", "text": "hello back"' in log
    events = logging_setup.EVENT_LOG.read_text()
    assert '"kind": "intent_classified"' in events


async def test_asking_details_about_a_specific_email_states_the_boundary(monkeypatch):
    """Found via chat.jsonl minutes after it went live: 'tell more about
    the Kotak email?' slipped past the body-boundary keywords and got the
    plain list a third time."""
    _mock_normalize(monkeypatch, "email.check", 'show me more details about the email titled "Kotak Credit Card"')

    async def fake_run_tool(call, **kwargs):
        return {"unread_estimate": 1, "messages": [{"from": "Kotak", "subject": "EMI", "date": "d"}]}

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)
    result = await orchestrator.handle_message(chat_id=0, raw_text="can you tell more about the Kotak email?")
    assert "can't open email contents" in result


async def test_qa_prompt_carries_the_generated_capability_brief(monkeypatch):
    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator, "capability_brief", lambda: "CAPABILITY_SENTINEL")
    captured = {}

    def fake_call(prompt, system="", **kwargs):
        captured["system"] = system
        return _FakeRouted(text="ok")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)
    await orchestrator.handle_message(chat_id=0, raw_text="can you book a cab?")
    assert "CAPABILITY_SENTINEL" in captured["system"]


async def test_history_render_clips_long_entries(monkeypatch):
    orchestrator._history[9].append(("user", "paste " * 500))
    block = orchestrator._history_block(9)
    assert len(block) < 700 and block.endswith("…")


async def test_qa_falls_back_to_cheap_when_frontier_is_exhausted(monkeypatch):
    """Seen live: Groq's 200k-token/day free cap ran out and qa.answer
    failed raw instead of degrading to the local model like
    classification does."""
    _mock_normalize(monkeypatch, "qa.answer")
    tiers = []

    def fake_call(prompt, system="", tier="cheap", **kwargs):
        tiers.append(tier)
        if tier == "frontier":
            raise orchestrator.router.ModelProviderError("429 rate_limit_exceeded")
        return _FakeRouted(text="local answer")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)
    result = await orchestrator.handle_message(chat_id=0, raw_text="hello there friend")
    assert result == "local answer"
    assert tiers == ["frontier", "cheap"]


async def test_provider_error_surfaces_clean_not_raw(monkeypatch):
    """Seen live: a Groq 429 dumped org ids and billing links into the
    chat. The user sees one clean line; the log gets the detail."""
    def always_fail(raw_text, tier="cheap", history=""):
        raise orchestrator.router.ModelProviderError(
            "groq failed: 429 {'org_id': 'org_SECRET', 'billing': 'https://console.groq.com'}"
        )

    monkeypatch.setattr(orchestrator, "normalize", always_fail)

    def cheap_also_fails(raw_text, tier="cheap", history=""):
        raise orchestrator.router.ModelProviderError("ollama down too")

    result = await orchestrator.handle_message(chat_id=0, raw_text="hello")
    assert "org_SECRET" not in result and "console.groq.com" not in result
    assert "having trouble" in result


async def test_stale_confirmation_expires_instead_of_executing(monkeypatch):
    """Deep-review safety catch: 'About to turn the AC ON' asked at noon
    must not execute on an unrelated 'yes' hours later."""
    _make_skill_confirm(monkeypatch, "reminders.create")
    _mock_normalize(monkeypatch, "reminders.create", "remind me to test")

    clock = {"t": 1000.0}
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: clock["t"])

    ask = await orchestrator.handle_message(chat_id=0, raw_text="remind me to test")
    assert "confirmation" in ask

    clock["t"] += 400  # 6.7 minutes later — past the 5-minute TTL
    ran = []
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: ran.append(1) or _FakeRouted(text="{}"))
    result = await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert "expired" in result
    assert ran == []  # the stale action never executed


async def test_fresh_confirmation_still_works_within_ttl(monkeypatch):
    from kyraan.tools import registry as reg

    _mock_normalize(monkeypatch, "calendar.create", "add test tomorrow 3pm to calendar")
    monkeypatch.setattr(
        orchestrator.router, "call",
        lambda **kwargs: _FakeRouted(
            text='{"title": "T", "start_iso": "2099-01-02T15:00:00+00:00", "end_iso": "2099-01-02T16:00:00+00:00", "location": null}'
        ),
    )

    async def fake_dispatch(spec, args):
        return {"id": "e", "link": None, "title": args["title"]}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    await orchestrator.handle_message(chat_id=0, raw_text="add test tomorrow 3pm to calendar")
    result = await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert "Event created" in result


async def test_low_confidence_messages_skip_extraction(monkeypatch):
    def unsure(raw_text, tier="cheap", history=""):
        return NormalizedIntent(intent="unknown", confidence=0.1, normalized_text=raw_text)

    monkeypatch.setattr(orchestrator, "normalize", unsure)
    calls = []

    async def counting(raw_text, context="", insist=False):
        calls.append(1)
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", counting)
    result = await orchestrator.handle_message(chat_id=0, raw_text="asdkjh qwe zzz ppqq")
    assert "rephrase" in result and calls == []


async def test_structured_extraction_falls_back_to_cheap(monkeypatch, isolated_store):
    """Reminder/event/window extraction goes frontier-first for exactness,
    local when the cloud tier is exhausted (seen live: 'in 45mins'
    extracted as a PAST time by the local model — frontier is the fix
    whenever it's available)."""
    _mock_normalize(monkeypatch, "reminders.create", "remind me to test in 45 minutes")
    tiers = []

    def fake_call(prompt, system="", tier="cheap", **kwargs):
        tiers.append(tier)
        if tier == "frontier":
            raise orchestrator.router.ModelProviderError("429")
        return _FakeRouted(text='{"text": "test", "when_iso": "2099-01-01T10:00:00+00:00"}')

    monkeypatch.setattr(orchestrator.router, "call", fake_call)
    orchestrator.scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=None)
    result = await orchestrator.handle_message(chat_id=0, raw_text="remind me to test in 45 minutes")
    assert "Reminder set" in result
    assert tiers == ["frontier", "cheap"]


async def test_past_event_start_is_refused_before_the_ask(monkeypatch, isolated_store):
    """Walkthrough v3: 'book a flight to delhi' misrouted into a confirm
    ask for 'Delhi Trip, Jan 2024' — a past start dies before any ask."""
    _mock_normalize(monkeypatch, "calendar.create", "add delhi trip to my calendar")
    monkeypatch.setattr(
        orchestrator.router, "call",
        lambda **kwargs: _FakeRouted(
            text='{"title": "Delhi Trip", "start_iso": "2024-01-22T12:00:00+00:00", "end_iso": "2024-01-29T13:00:00+00:00", "location": null}'
        ),
    )
    result = await orchestrator.handle_message(chat_id=0, raw_text="add delhi trip to my calendar")
    assert "in the past" in result
    assert "reply \"yes\"" not in result  # no confirm ask was created
    assert orchestrator._pending_confirmations == {}


async def test_hallucinated_normalization_is_replaced_with_raw_text(monkeypatch):
    """Degraded classifier turned 'Do you know my father?' into the ANSWER
    'I don't have any information about your family members.' — qa then
    answered the hallucination. Zero-overlap rewrites now revert to raw."""
    def bad_normalize(raw_text, tier="cheap", history=""):
        return NormalizedIntent(intent="qa.answer", confidence=1.0,
                                normalized_text="I don't have any information about your family members.")

    monkeypatch.setattr(orchestrator, "normalize", bad_normalize)
    captured = {}

    def fake_call(prompt, system="", **kwargs):
        captured["prompt"] = prompt
        return _FakeRouted(text="ok")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)
    await orchestrator.handle_message(chat_id=0, raw_text="Do you know my father?")
    assert captured["prompt"] == "Do you know my father?"  # raw, not the hallucination


async def test_stated_clock_time_beats_model_extraction(monkeypatch, isolated_store):
    """Seen live twice: '9pm' extracted as 8:00 PM, '8pm' as 20:49. An
    explicit am/pm time in the message deterministically corrects the
    extracted clock."""
    _mock_normalize(monkeypatch, "reminders.create", "remind me to call mom at 9pm tonight")
    monkeypatch.setattr(
        orchestrator.router, "call",
        lambda **kwargs: _FakeRouted(text='{"text": "call mom", "when_iso": "2099-01-01T20:00:00+00:00"}'),
    )
    orchestrator.scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=None)
    result = await orchestrator.handle_message(chat_id=0, raw_text="remind me to call mom at 9pm tonight")
    assert "9:00 PM" in result  # corrected from the model's 8pm
    stored = orchestrator.scheduler.store.list_pending(0)[0]
    assert "T21:00:00" in stored.when_iso


async def test_no_stated_clock_leaves_extraction_alone(monkeypatch, isolated_store):
    _mock_normalize(monkeypatch, "reminders.create", "remind me to call mom in 45 minutes")
    monkeypatch.setattr(
        orchestrator.router, "call",
        lambda **kwargs: _FakeRouted(text='{"text": "call mom", "when_iso": "2099-01-01T20:15:00+00:00"}'),
    )
    orchestrator.scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=None)
    result = await orchestrator.handle_message(chat_id=0, raw_text="remind me to call mom in 45 minutes")
    assert "8:15 PM" in result  # untouched


async def test_typo_correction_rewrites_are_accepted(monkeypatch):
    """The rewrite guard rejected 'wat tym is it' -> 'what time is it' — a
    CORRECT typo fix. Textual similarity is the second legitimate path."""
    def typo_fix(raw_text, tier="cheap", history=""):
        return NormalizedIntent(intent="qa.answer", confidence=0.96, normalized_text="what time is it")

    monkeypatch.setattr(orchestrator, "normalize", typo_fix)
    captured = {}

    def fake_call(prompt, system="", **kwargs):
        captured["prompt"] = prompt
        return _FakeRouted(text="9pm")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)
    await orchestrator.handle_message(chat_id=0, raw_text="wat tym is it")
    assert captured["prompt"] == "what time is it"  # correction kept


async def test_pending_facts_reach_the_qa_prompt(monkeypatch):
    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.memory_store, "load_pending_facts", lambda **kwargs: "- His name is biren roy")
    captured = {}

    def fake_call(prompt, system="", **kwargs):
        captured["system"] = system
        return _FakeRouted(text="Deven Roy is your father (awaiting your review).")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)
    await orchestrator.handle_message(chat_id=0, raw_text="who is biren?")
    assert "- His name is biren roy" in captured["system"]
    assert "awaiting the" in captured["system"]  # honest framing instruction


async def test_control_intent_without_a_device_answers_conversationally(monkeypatch):
    """Seen live twice: 'let me fix you' misrouted to home.control and got
    'Should the AC go on or off?'. No device mention = no switch talk."""
    _mock_normalize(monkeypatch, "home.control", "let me fix you")
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="Tell me what's wrong and I'll improve."))
    result = await orchestrator.handle_message(chat_id=0, raw_text="let me fix you")
    assert "AC" not in result and "improve" in result


async def test_incomplete_fragment_waits_instead_of_answering(monkeypatch):
    """Seen live: 'tomorrow morning' (first fragment of a slow burst) got
    an irrelevant morning-brief answer. A fragment gets a listening
    prompt — deterministic, no qa call — and stays in history so the
    next message completes the thought."""
    _mock_normalize(monkeypatch, "incomplete", "tomorrow morning")

    def explode(**kwargs):
        raise AssertionError("no model call for a fragment")

    monkeypatch.setattr(orchestrator.router, "call", explode)
    calls = []

    async def counting(raw_text, context="", insist=False):
        calls.append(1)
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", counting)
    result = await orchestrator.handle_message(chat_id=0, raw_text="tomorrow morning")
    assert "listening" in result.lower()
    assert calls == []  # extraction skipped too
    assert "user: tomorrow morning" in orchestrator._history_block(0)  # kept for the follow-up


async def test_bare_time_phrase_is_deterministically_patient(monkeypatch):
    """'tomorrow morning' became a literal reminder named 'tomorrow
    morning' at 6 AM — the classifier can't be trusted with fragments, so
    detection is deterministic and no model is consulted at all."""
    def explode(**kwargs):
        raise AssertionError("no model call for a time fragment")

    monkeypatch.setattr(orchestrator.router, "call", explode)
    monkeypatch.setattr(orchestrator, "normalize", explode)
    for fragment in ("tomorrow morning", "at 9", "tonight after dinner", "next week"):
        result = await orchestrator.handle_message(chat_id=0, raw_text=fragment)
        assert "listening" in result.lower()


def test_time_fragment_detector_boundaries():
    assert orchestrator.is_time_fragment("tomorrow morning")
    assert orchestrator.is_time_fragment("at 9 pm")
    assert not orchestrator.is_time_fragment("remind me at 9")
    assert not orchestrator.is_time_fragment("call the plumber tomorrow")
    assert not orchestrator.is_time_fragment("what time is it")


async def test_reminder_with_time_phrase_text_asks_for_the_task(monkeypatch, isolated_store):
    _mock_normalize(monkeypatch, "reminders.create", "remind me tomorrow morning at 6am please do it")
    monkeypatch.setattr(
        orchestrator.router, "call",
        lambda **kwargs: _FakeRouted(text='{"text": "tomorrow morning", "when_iso": "2099-01-01T06:00:00+00:00"}'),
    )
    orchestrator.scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=None)
    result = await orchestrator.handle_message(chat_id=0, raw_text="remind me tomorrow morning at 6am please do it")
    assert "Remind you about what" in result
    assert orchestrator.scheduler.store.list_pending(0) == []


async def test_burst_planner_combines_one_thought(monkeypatch):
    """The owner's spec: evaluate the burst together, then decide. A
    fragmented single thought merges into one request and one reply."""
    plans = iter([
        _FakeRouted(text='{"requests": ["remind me to call the plumber tomorrow at 9am"]}'),
    ])
    handled = []

    def fake_call(prompt, system="", **kwargs):
        if "quick messages as ONE burst" in system:
            return next(plans)
        return _FakeRouted(text="ok")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)

    async def fake_handle(chat_id, text):
        handled.append(text)
        return "one reply"

    monkeypatch.setattr(orchestrator, "handle_message", fake_handle)
    results = await orchestrator.handle_burst(1, ["tomorrow morning", "i need to call the plumber", "remind me at 9am"])
    assert handled == ["remind me to call the plumber tomorrow at 9am"]
    assert results == [(2, "one reply")]  # ONE reply, quoted onto the last message


async def test_burst_multiple_requests_compose_one_reply(monkeypatch):
    """Distinct asks are executed distinctly but ANSWERED as one composed
    message — a human never sends five replies (seen live: a 5-fragment
    burst got 5 scattered answers)."""
    def explode(**kwargs):
        raise AssertionError("questions heuristic needs no model")

    monkeypatch.setattr(orchestrator.router, "call", explode)
    handled = []

    async def fake_handle(chat_id, text):
        handled.append(text)
        return f"answer to {text}"

    monkeypatch.setattr(orchestrator, "handle_message", fake_handle)
    results = await orchestrator.handle_burst(1, ["is the AC on?", "any new emails?"])
    assert handled == ["is the AC on?", "any new emails?"]
    assert len(results) == 1  # one composed reply
    idx, reply = results[0]
    assert idx == 1 and "answer to is the AC on?" in reply and "answer to any new emails?" in reply


async def test_burst_planner_falls_back_to_plain_merge(monkeypatch):
    def broken_call(prompt, system="", **kwargs):
        raise orchestrator.router.ModelProviderError("down")

    monkeypatch.setattr(orchestrator.router, "call", broken_call)
    handled = []

    async def fake_handle(chat_id, text):
        handled.append(text)
        return "fallback reply"

    monkeypatch.setattr(orchestrator, "handle_message", fake_handle)
    results = await orchestrator.handle_burst(1, ["do a thing", "b thing"])
    assert handled == ["do a thing\nb thing"]
    assert results == [(1, "fallback reply")]


async def test_filler_folds_into_minimal_requests(monkeypatch):
    """The live 5-fragment casual burst: greetings and filler fold away;
    the plan is the minimal request set; ONE composed reply comes back."""
    def fake_call(prompt, system="", **kwargs):
        return _FakeRouted(text='{"requests": ["hi! how are you", '
                                '"check my unread emails and tell me tomorrow\'s plan"]}')

    monkeypatch.setattr(orchestrator.router, "call", fake_call)
    handled = []

    async def fake_handle(chat_id, text):
        handled.append(text)
        return f"[{text}]"

    monkeypatch.setattr(orchestrator, "handle_message", fake_handle)
    results = await orchestrator.handle_burst(
        1, ["hey hi", "how are you?", "let cehck tomorrow email", "lety me kow", "what is plan"])
    assert len(handled) == 2  # five fragments, two real requests
    assert len(results) == 1  # one reply
    assert results[0][0] == 4  # quoted on the last fragment


def test_thought_open_reads_message_shape_like_a_human():
    """The channel's substitute for the typing indicator Telegram never
    gives bots: a message that trails off on a connector, opens with a
    continuation word, or is a bare time phrase means more is coming."""
    assert orchestrator.thought_open("to buy something")            # leading connector
    assert orchestrator.thought_open("I have an idea to build a")   # trailing article
    assert orchestrator.thought_open("very important")              # intensifier addendum
    assert orchestrator.thought_open("tomorrow morning")            # time fragment
    assert orchestrator.thought_open("I'll go there and,")          # trailing comma
    assert orchestrator.thought_open("on my smoke havite")           # leading preposition
    assert not orchestrator.thought_open("what is the plan?")
    assert not orchestrator.thought_open("hello")
    assert not orchestrator.thought_open("turn off the AC.")
    assert not orchestrator.thought_open("today moring I have to go to siliguri")


async def test_burst_superseded_when_a_fragment_lands_mid_planning(monkeypatch):
    """A fragment arriving while the plan is still being made retracts the
    draft BEFORE anything runs — the channel re-plans with the full
    thought (the human move: stop typing, read, rethink)."""
    import asyncio

    event = asyncio.Event()

    def fake_call(prompt, system="", **kwargs):
        event.set()  # the late fragment lands during planning
        return _FakeRouted(text='{"requests": ["do the thing"]}')

    monkeypatch.setattr(orchestrator.router, "call", fake_call)

    async def must_not_run(chat_id, text):
        raise AssertionError("no request may execute after a supersede")

    monkeypatch.setattr(orchestrator, "handle_message", must_not_run)
    with pytest.raises(orchestrator.BurstSuperseded):
        await orchestrator.handle_burst(
            1, ["today morning I have to go", "to siliguri"], superseded=event)


async def test_reminder_intent_without_remind_wording_is_demoted(monkeypatch):
    """Seen live: 'to buy something' — a fragment of a story about the
    user's morning — became a junk reminder at 12:00 AM. reminders.create
    without any remind-ish wording is the classifier over-reaching; the
    message is answered as conversation and nothing is scheduled."""
    _mock_normalize(monkeypatch, "reminders.create", "to buy something")

    created = []
    monkeypatch.setattr(orchestrator.scheduler, "create_reminder",
                        lambda *a, **k: created.append(1))

    async def fake_answer(chat_id, text):
        return "Sounds like a busy morning."

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator, "_answer", fake_answer)
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    result = await orchestrator.handle_message(chat_id=0, raw_text="to buy something")
    assert "busy morning" in result
    assert created == []  # no reminder was ever attempted


async def test_rejected_rewrite_discredits_the_intent_too(monkeypatch):
    """Seen live: 'on my smoke havite' rewrote to 'what's the status of my
    humidifier' — the guard rejected the rewrite but kept the home.query
    intent, and the user got the full AC dump. A hallucinated rewrite and
    its intent are one judgment: both fall together."""
    _mock_normalize(monkeypatch, "home.query", "what's the status of my humidifier")

    async def fake_answer(chat_id, text):
        return f"conversational: {text}"

    async def must_not_run(chat_id, text):
        raise AssertionError("home.query must not execute on a discredited intent")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator, "_answer", fake_answer)
    monkeypatch.setattr(orchestrator, "_home_query", must_not_run)
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    result = await orchestrator.handle_message(chat_id=0, raw_text="on my smoke havite")
    # Both the rewrite and the intent were dropped: the raw words are
    # answered as conversation.
    assert result == "conversational: on my smoke havite"


async def test_home_query_without_any_home_word_is_demoted(monkeypatch):
    """A home question names something in the home; without any such word
    the classification is a guess and must converse, not dump status."""
    _mock_normalize(monkeypatch, "home.query", "on my smoke habit")

    async def fake_answer(chat_id, text):
        return "Let's talk about that."

    async def must_not_run(chat_id, text):
        raise AssertionError("no home tool for a guess")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator, "_answer", fake_answer)
    monkeypatch.setattr(orchestrator, "_home_query", must_not_run)
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    result = await orchestrator.handle_message(chat_id=0, raw_text="on my smoke habit")
    assert result == "Let's talk about that."


async def test_verbatim_repetition_retries_then_admits(monkeypatch):
    """Seen live: 'I can't book cabs yet.' sent to three different
    questions. A reply identical to a recent assistant reply gets one
    retry naming the problem; a stuck model admits it instead of looping."""
    _mock_normalize(monkeypatch, "qa.answer", "on what")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)

    import time as _time

    # Retry produces something fresh -> the fresh reply wins.
    orchestrator._history[51].clear()
    orchestrator._history[51].append(("assistant", "I can't book cabs yet."))
    orchestrator._last_reply_at[51] = _time.monotonic()  # live exchange
    replies = iter([_FakeRouted(text="I can't book cabs yet."),
                    _FakeRouted(text="Sorry — what would you like help with?")])
    monkeypatch.setattr(orchestrator.router, "call", lambda **kw: next(replies))
    result = await orchestrator.handle_message(chat_id=51, raw_text="on what")
    assert result.startswith("Sorry — what would you like help with?")

    # Retry repeats too -> honest admission, never the loop.
    orchestrator._history[52].clear()
    orchestrator._history[52].append(("assistant", "I can't book cabs yet."))
    orchestrator._last_reply_at[52] = _time.monotonic()
    monkeypatch.setattr(orchestrator.router, "call",
                        lambda **kw: _FakeRouted(text="I can't book cabs yet."))
    result = await orchestrator.handle_message(chat_id=52, raw_text="on what")
    assert "repeating myself" in result


def test_home_word_guard_still_passes_real_home_questions():
    assert orchestrator._mentions_home("is the AC on?")
    assert orchestrator._mentions_home("how humid is it inside")
    assert orchestrator._mentions_home("what's the bedroom temperature")
    assert orchestrator._mentions_home("how much power is it drawing")
    assert orchestrator._mentions_home("did I leave it off")
    assert not orchestrator._mentions_home("on my smoke havite")
    assert not orchestrator._mentions_home("what's your favorite place?")


def _cancel_flow_fixtures(monkeypatch):
    """Shared setup for calendar.cancel tests: window extraction + a
    3-event calendar behind the real kernel gate."""
    from kyraan.tools import registry as reg

    monkeypatch.setattr(orchestrator.router, "call", lambda **kw: _FakeRouted(
        text='{"start_iso": "2099-01-02T00:00:00+00:00", "end_iso": "2099-01-02T23:59:59+00:00", "label": "today"}'))

    events = [
        {"id": "ev-train", "title": "Train to NJP", "start": "2099-01-02T10:30:00+00:00",
         "end": "2099-01-02T11:00:00+00:00", "all_day": False, "location": None, "recurring": False},
        {"id": "ev-test", "title": "Test Event", "start": "2099-01-02T15:00:00+00:00",
         "end": "2099-01-02T15:30:00+00:00", "all_day": False, "location": None, "recurring": False},
        {"id": "ev-call", "title": "Call of client", "start": "2099-01-02T15:30:00+00:00",
         "end": "2099-01-02T16:00:00+00:00", "all_day": False, "location": None, "recurring": False},
    ]
    deleted = []

    async def fake_dispatch(spec, args):
        if spec.name == "calendar.list_events":
            return events
        deleted.append(args["event_id"])
        return {"id": args["event_id"], "deleted": True, "already_gone": False}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    return deleted


async def test_cancel_all_events_asks_naming_every_event_then_deletes(monkeypatch):
    """The live disaster this exists for: 'cancel all events' got a fake
    promise, then 'yes right now' created a junk event titled 'Cancel All
    Events'. Now: the ask NAMES the exact events, nothing is touched
    before the yes, and the yes deletes precisely those."""
    _mock_normalize(monkeypatch, "calendar.cancel", "cancel all events today")
    deleted = _cancel_flow_fixtures(monkeypatch)

    ask = await orchestrator.handle_message(chat_id=0, raw_text="cancel all events today")
    assert "DELETE 3 event(s)" in ask
    assert "Train to NJP" in ask and "Test Event" in ask and "Call of client" in ask
    assert deleted == []  # nothing removed before the yes

    result = await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert deleted == ["ev-train", "ev-test", "ev-call"]
    assert "Deleted from your calendar" in result and "Test Event" in result


async def test_cancel_by_title_targets_only_the_match(monkeypatch):
    _mock_normalize(monkeypatch, "calendar.cancel", "cancel the test event")
    deleted = _cancel_flow_fixtures(monkeypatch)

    ask = await orchestrator.handle_message(chat_id=0, raw_text="cancel the test event")
    assert "DELETE 1 event(s)" in ask and "Test Event" in ask
    assert "Train to NJP" not in ask

    await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert deleted == ["ev-test"]


async def test_cancel_with_no_match_lists_and_asks_which(monkeypatch):
    _mock_normalize(monkeypatch, "calendar.cancel", "cancel the dentist appointment")
    deleted = _cancel_flow_fixtures(monkeypatch)

    result = await orchestrator.handle_message(chat_id=0, raw_text="cancel the dentist appointment")
    assert "couldn't match" in result and "Which one" in result
    assert "Train to NJP" in result  # shows what IS there
    assert deleted == []


async def test_cancel_no_denies_without_deleting(monkeypatch):
    _mock_normalize(monkeypatch, "calendar.cancel", "cancel all events today")
    deleted = _cancel_flow_fixtures(monkeypatch)

    await orchestrator.handle_message(chat_id=0, raw_text="cancel all events today")
    result = await orchestrator.handle_message(chat_id=0, raw_text="no")
    assert "cancelled, nothing was done" in result
    assert deleted == []


async def test_bare_cancel_asks_which_never_sweeps(monkeypatch):
    """'can you cancel' with no object escalated straight to a
    DELETE-4-events ask (live 2026-08-26). No object and no 'all' ->
    list and ask; only an explicit all/everything sweeps the window."""
    _mock_normalize(monkeypatch, "calendar.cancel", "can you cancel")
    deleted = _cancel_flow_fixtures(monkeypatch)

    result = await orchestrator.handle_message(chat_id=0, raw_text="can you cancel")
    assert "Cancel which event?" in result and "Train to NJP" in result
    assert "DELETE" not in result
    assert deleted == []


async def test_meta_question_about_a_listing_answers_instead_of_reprinting(monkeypatch):
    """'are these latest emails' re-ran email.check and reprinted the
    identical listing (live, twice in one session). Identical read-reply
    + meta-question shape -> answer the question via qa."""
    _mock_normalize(monkeypatch, "email.check", "are these latest emails")
    listing = "You have about 201 unread. Latest:\n- A: x\n- B: y"

    async def fake_check(chat_id, text=""):
        return listing

    async def fake_answer(chat_id, text):
        return "Yes — those are the five most recent unread ones."

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator, "_check_email", fake_check)
    monkeypatch.setattr(orchestrator, "_answer", fake_answer)
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    orchestrator._last_sent_reply[61] = listing  # previous turn showed it

    result = await orchestrator.handle_message(chat_id=61, raw_text="are these latest emails")
    assert result.startswith("Yes — those are the five most recent")

    # A straight re-ask ("check emails") is NOT a meta-question — the
    # listing prints again, truthfully.
    _mock_normalize(monkeypatch, "email.check", "check emails")
    result = await orchestrator.handle_message(chat_id=61, raw_text="check emails")
    assert result.startswith("You have about 201 unread")


def test_history_seeds_from_chat_log_after_restart(tmp_path, monkeypatch):
    """Restart amnesia, seen live: 5 minutes after a service restart,
    'are those the latest emails?' was judged against an EMPTY history
    and got a fabricated 'No'. The disk log is the source of truth."""
    import json as j
    from kyraan.control_plane import logging_setup

    log = tmp_path / "chat.jsonl"
    entries = [
        {"ts": "t", "chat_id": 71, "role": "user", "text": "check emails"},
        {"ts": "t", "chat_id": 71, "role": "assistant", "text": "Latest: A, B, C"},
        {"ts": "t", "chat_id": 71, "role": "proactive", "text": "Reminder: call the plumber"},
        {"ts": "t", "chat_id": 72, "role": "user", "text": "other chat"},
    ]
    log.write_text("\n".join(j.dumps(e) for e in entries))
    monkeypatch.setattr(logging_setup, "CHAT_LOG", log)
    orchestrator._history.pop(71, None)
    orchestrator._history.pop(72, None)

    orchestrator.seed_history_from_log()
    block = orchestrator._history_block(71)
    assert "Latest: A, B, C" in block                 # the listing survives restart
    assert "call the plumber" in block                # proactive sends count as assistant
    assert "other chat" not in block                  # per-chat isolation
    orchestrator._history.pop(71, None)
    orchestrator._history.pop(72, None)


def test_seed_never_clobbers_a_live_conversation(tmp_path, monkeypatch):
    import json as j
    from kyraan.control_plane import logging_setup

    log = tmp_path / "chat.jsonl"
    log.write_text(j.dumps({"ts": "t", "chat_id": 73, "role": "user", "text": "old stuff"}))
    monkeypatch.setattr(logging_setup, "CHAT_LOG", log)
    orchestrator._history.pop(73, None)
    orchestrator._history[73].append(("user", "live message"))

    orchestrator.seed_history_from_log()
    block = orchestrator._history_block(73)
    assert "live message" in block and "old stuff" not in block
    orchestrator._history.pop(73, None)


def test_meta_detection_covers_complaints_and_questions():
    assert orchestrator._is_meta_question("are these latest emails")
    assert orchestrator._is_meta_question("is that all?")
    assert orchestrator._is_meta_question("these emails are already shared by u")
    assert orchestrator._is_meta_question("you showed this again")
    assert not orchestrator._is_meta_question("check emails")
    assert not orchestrator._is_meta_question("any new emails?")
    assert not orchestrator._is_meta_question("cancel all events today")


async def test_repeated_greeting_is_not_a_repetition_loop(monkeypatch):
    """Found live the morning after history seeding: 'helo' matched last
    night's greeting reply and got the I'm-repeating-myself apology. A
    greeting answered with the same greeting — especially with no live
    exchange this process — is human, not a loop."""
    _mock_normalize(monkeypatch, "qa.answer", "helo")
    monkeypatch.setattr(orchestrator.router, "call",
                        lambda **kw: _FakeRouted(text="Hello! How can I assist you today?"))

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)

    # Seeded history holds the same greeting from last night; no reply has
    # been sent by THIS process (no _last_reply_at entry).
    orchestrator._history[54].clear()
    orchestrator._history[54].append(("assistant", "Hello! How can I assist you today?"))
    orchestrator._last_reply_at.pop(54, None)

    result = await orchestrator.handle_message(chat_id=54, raw_text="helo")
    assert result.startswith("Hello! How can I assist you today?")

    # Even mid-conversation, a greeting repeat stays exempt.
    import time as _time
    orchestrator._last_reply_at[54] = _time.monotonic()
    result = await orchestrator.handle_message(chat_id=54, raw_text="hello there")
    assert result.startswith("Hello! How can I assist you today?")
    orchestrator._history.pop(54, None)


def _seed_review_queue(monkeypatch, tmp_path):
    """Two real-shaped proposals in an isolated pending dir + memory tree."""
    from kyraan.memory import store as mstore

    memory_root = tmp_path / "memory"
    pending = memory_root / "pending_review"
    pending.mkdir(parents=True)
    monkeypatch.setattr(mstore, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(mstore, "PENDING_DIR", pending)
    (pending / "a__people__father.md").write_text(
        "---\ntarget: people/father.md\nsource_statement: 'x'\n---\n\n- Father's name is Tarun Roy\n")
    (pending / "b__people__reminder.md").write_text(
        "---\ntarget: people/reminder.md\nsource_statement: 'y'\n---\n\n- User asked to call the plumber\n")
    return memory_root, pending


async def test_review_memory_lists_then_mixed_decision_promotes_and_rejects(monkeypatch, tmp_path):
    """The live failure this replaces: 'reviewed and confirmed' got 'I'll
    mark the remaining items as saved now' — a false claim with no save
    behind it. Now the review flow lists the queue and the deterministic
    approve/reject reply actually moves the files."""
    from kyraan.memory import store as mstore

    memory_root, pending = _seed_review_queue(monkeypatch, tmp_path)
    _mock_normalize(monkeypatch, "memory.review", "review memory")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    orchestrator._pending_reviews.pop(0, None)

    listing = await orchestrator.handle_message(chat_id=0, raw_text="review memory")
    assert "1. Father's name is Tarun Roy" in listing
    assert "2. User asked to call the plumber" in listing

    result = await orchestrator.handle_message(chat_id=0, raw_text="approve 1 reject 2")
    assert "Saved to memory: Father's name is Tarun Roy" in result
    assert "Rejected: User asked to call the plumber" in result
    assert "Tarun Roy" in (memory_root / "people" / "father.md").read_text()
    assert list(pending.glob("*.md")) == []  # queue drained


async def test_unrelated_reply_leaves_the_review_queue_untouched(monkeypatch, tmp_path):
    _memory_root, pending = _seed_review_queue(monkeypatch, tmp_path)
    _mock_normalize(monkeypatch, "memory.review", "review memory")

    async def no_facts(raw_text, context="", insist=False):
        return []

    async def fake_answer(chat_id, text):
        return "Sure — the weather, you say?"

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    monkeypatch.setattr(orchestrator, "_answer", fake_answer)
    orchestrator._pending_reviews.pop(0, None)

    await orchestrator.handle_message(chat_id=0, raw_text="review memory")
    _mock_normalize(monkeypatch, "qa.answer", "what about the weather")
    await orchestrator.handle_message(chat_id=0, raw_text="what about the weather")
    assert len(list(pending.glob("*.md"))) == 2  # nothing implicitly approved
    assert 0 not in orchestrator._pending_reviews  # session dropped


def test_review_decision_parser_boundaries():
    parse = orchestrator._parse_review_decision
    assert parse("approve all", 3) == ([0, 1, 2], [])
    assert parse("approve 1, 3", 3) == ([0, 2], [])
    assert parse("reject 2", 3) == ([], [1])
    assert parse("approve 1 reject 2", 3) == ([0], [1])
    assert parse("approve 2 reject 2", 3) == ([], [1])  # conflict: stays unsaved
    assert parse("yes", 3) is None            # plain yes is a confirm word, not a review decision
    assert parse("what about 2?", 3) is None  # not a decision at all


async def test_explicit_save_that_extracts_nothing_says_so(monkeypatch, tmp_path):
    """Seen live: 'save the kiaan age' produced no proposal and no
    acknowledgement of the failure. An explicit save must either queue a
    fact (📝 line) or admit it couldn't."""
    from kyraan.memory import store as mstore

    memory_root = tmp_path / "memory"
    (memory_root / "pending_review").mkdir(parents=True)
    monkeypatch.setattr(mstore, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(mstore, "PENDING_DIR", memory_root / "pending_review")
    _mock_normalize(monkeypatch, "qa.answer", "save the kiaan age")

    async def fake_answer(chat_id, text):
        return "Okay."

    calls = []

    async def empty_extraction(raw_text, context="", insist=False):
        calls.append(insist)
        return []

    monkeypatch.setattr(orchestrator, "_answer", fake_answer)
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", empty_extraction)
    result = await orchestrator.handle_message(chat_id=0, raw_text="save the kiaan age")
    assert calls == [True]                      # the insist path was used
    assert "couldn't distill a durable fact" in result

    # An ordinary statement extracting nothing stays silent, as before.
    _mock_normalize(monkeypatch, "qa.answer", "nice weather today")
    result = await orchestrator.handle_message(chat_id=0, raw_text="nice weather today")
    assert calls == [True, False]
    assert "couldn't distill" not in result


async def test_review_command_never_runs_extraction(monkeypatch, tmp_path):
    """'yes save it' (a queue command) got the couldn't-distill warning
    glued under the review list — extraction must not run on
    memory.review messages at all."""
    _seed_review_queue(monkeypatch, tmp_path)
    _mock_normalize(monkeypatch, "memory.review", "yes save it")
    calls = []

    async def counting(raw_text, context="", insist=False):
        calls.append(1)
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", counting)
    orchestrator._pending_reviews.pop(0, None)
    result = await orchestrator.handle_message(chat_id=0, raw_text="yes save it")
    assert "Facts awaiting your review" in result
    assert "couldn't distill" not in result
    assert calls == []
    orchestrator._pending_reviews.pop(0, None)


async def test_explicit_save_of_an_already_queued_fact_points_to_review(monkeypatch, tmp_path):
    """Dedup is not failure: when the fact is already pending, the note
    must say so instead of 'couldn't distill' (seen live)."""
    from kyraan.memory import store as mstore

    _seed_review_queue(monkeypatch, tmp_path)  # queue holds the Tarun fact
    _mock_normalize(monkeypatch, "qa.answer", "you should save ganak name")

    async def fake_answer(chat_id, text):
        return "Okay."

    async def empty_extraction(raw_text, context="", insist=False):
        return []  # dedup ate it

    monkeypatch.setattr(orchestrator, "_answer", fake_answer)
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", empty_extraction)
    result = await orchestrator.handle_message(chat_id=0, raw_text="you should save ganak name")
    assert "already in the review queue" in result
    assert "couldn't distill" not in result


def test_history_block_tiers_old_entries(monkeypatch):
    """Token thrift without information loss: the last 8 entries keep the
    full clip (they carry follow-up context); older ones keep their gist."""
    orchestrator._history.pop(95, None)
    long = "x" * 500
    for i in range(12):
        orchestrator._history[95].append(("user", f"{i}:{long}"))

    block = orchestrator._history_block(95, clip=600, older_clip=100)
    lines = block.splitlines()
    assert len(lines) == 12
    assert all(len(l) <= 110 for l in lines[:4])   # old: tightened
    assert all(len(l) > 400 for l in lines[4:])    # recent 8: full
    # default behavior unchanged
    assert all(len(l) > 400 for l in orchestrator._history_block(95).splitlines())
    orchestrator._history.pop(95, None)


async def test_email_redaction_survives_a_restart(monkeypatch):
    """PROPERTY (review P1): sender/subject metadata must never reach a
    cloud prompt — including via chat.jsonl -> history seeding after a
    restart. The local log keeps the full text (that's the audit); the
    seeded history must carry only the placeholder."""
    import json as j
    from kyraan.control_plane import logging_setup

    _mock_normalize(monkeypatch, "email.check")

    async def fake_run_tool(call, **kwargs):
        return {"unread_estimate": 1, "messages": [
            {"from": '"Suman Das" <s@x.com>', "subject": "Invoice pending", "date": "d"}]}

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    monkeypatch.setattr(orchestrator, "_cloud_tier_in_use", lambda: True)

    await orchestrator.handle_message(chat_id=88, raw_text="any new emails?")

    log_lines = [j.loads(l) for l in logging_setup.CHAT_LOG.read_text().splitlines()]
    full = next(e for e in log_lines if e["role"] == "assistant" and e["chat_id"] == 88)
    assert "Invoice pending" in full["text"]            # local audit keeps everything
    assert "Invoice" not in full.get("cloud_text", "")  # the cloud twin does not

    # THE RESTART: wipe memory, reseed from disk.
    orchestrator._history.pop(88, None)
    orchestrator.seed_history_from_log()
    block = orchestrator._history_block(88)
    assert "Invoice pending" not in block and "Suman Das" not in block
    assert "email" in block.lower()                     # the placeholder survived
    orchestrator._history.pop(88, None)


async def test_unknown_outcome_warning_reaches_the_user(monkeypatch):
    """PROPERTY (review P1): the write-timeout receipt exists to prevent
    unsafe retries — no catch-all may replace it with a generic error."""
    _mock_normalize(monkeypatch, "home.control", "turn on the AC")

    calls = {"n": 0}

    async def fake_run_tool(call, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise orchestrator.ConfirmationRequired(call.tool_name, call.args)
        raise orchestrator.kernel.ToolFailed(
            "home.turn_on timed out — the command MAY still have gone through; "
            "check the actual state before retrying")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    await orchestrator.handle_message(chat_id=0, raw_text="turn on the AC")
    result = await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert "MAY still have gone through" in result
    assert "Something went wrong" not in result


async def test_cancel_all_ask_is_capped_at_the_kernel_batch_budget(monkeypatch, tmp_path):
    """Review P2: never confirm more deletions than can actually run."""
    from kyraan.tools import registry as reg

    monkeypatch.setattr(orchestrator.router, "call", lambda **kw: _FakeRouted(
        text='{"start_iso": "2099-01-02T00:00:00+00:00", "end_iso": "2099-01-02T23:59:59+00:00", "label": "today"}'))
    events = [{"id": f"ev{i}", "title": f"Event {i}", "start": f"2099-01-02T{8+i:02d}:00:00+00:00",
               "end": f"2099-01-02T{9+i:02d}:00:00+00:00", "all_day": False,
               "location": None, "recurring": False} for i in range(12)]

    async def fake_dispatch(spec, args):
        if spec.name == "calendar.list_events":
            return events
        return {"id": args["event_id"], "deleted": True, "already_gone": False}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    _mock_normalize(monkeypatch, "calendar.cancel", "cancel all events today")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    ask = await orchestrator.handle_message(chat_id=0, raw_text="cancel all events today")
    assert "DELETE 8 event(s)" in ask and "4 more matched" in ask

    result = await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert result.count("Event ") == 8  # exactly the confirmed batch ran


def test_pre_upgrade_email_logs_are_redacted_at_seed_time(tmp_path, monkeypatch):
    """Review P1: chat.jsonl entries written BEFORE the cloud_text twin
    existed carry full listings — seeding must recognize the legacy
    templates and substitute the placeholder."""
    import json as j
    from kyraan.control_plane import logging_setup

    log = tmp_path / "chat.jsonl"
    log.write_text("\n".join([
        j.dumps({"ts": "t", "chat_id": 81, "role": "user", "text": "check emails"}),
        j.dumps({"ts": "t", "chat_id": 81, "role": "assistant",
                 "text": "You have about 201 unread. Latest:\n- Suman Das: Invoice pending"}),
        j.dumps({"ts": "t", "chat_id": 81, "role": "assistant",
                 "text": "Hello! How can I assist you today?"}),
    ]))
    monkeypatch.setattr(logging_setup, "CHAT_LOG", log)
    orchestrator._history.pop(81, None)
    orchestrator.seed_history_from_log()
    block = orchestrator._history_block(81)
    assert "Invoice pending" not in block and "Suman Das" not in block
    assert "[showed the unread email summary]" in block
    assert "Hello! How can I assist" in block   # ordinary replies untouched
    orchestrator._history.pop(81, None)


async def test_timed_out_delete_reports_unknown_not_untouched(monkeypatch):
    """Round-5 P2: a timed-out delete may have succeeded — the receipt
    separates deleted / outcome-UNKNOWN / untouched instead of inviting a
    double-delete."""
    from kyraan.tools import registry as reg

    monkeypatch.setattr(orchestrator.router, "call", lambda **kw: _FakeRouted(
        text='{"start_iso": "2099-01-02T00:00:00+00:00", "end_iso": "2099-01-02T23:59:59+00:00", "label": "today"}'))
    events = [{"id": f"ev{i}", "title": f"Event {i}", "start": f"2099-01-02T{8+i:02d}:00:00+00:00",
               "end": f"2099-01-02T{9+i:02d}:00:00+00:00", "all_day": False,
               "location": None, "recurring": False} for i in range(3)]

    async def fake_dispatch(spec, args):
        if spec.name == "calendar.list_events":
            return events
        if args["event_id"] == "ev1":
            raise reg.TransientToolError("slow")  # will become a timeout-ish failure
        return {"id": args["event_id"], "deleted": True, "already_gone": False}

    calls = {"n": 0}

    async def fake_run_tool(call, **kwargs):
        if call.tool_name == "calendar.list_events":
            return events
        if call.args["event_id"] == "ev1":
            raise orchestrator.kernel.ToolFailed(
                "calendar.delete_event timed out — the command MAY still have gone through; "
                "check the actual state before retrying")
        return {"id": call.args["event_id"], "deleted": True, "already_gone": False}

    _mock_normalize(monkeypatch, "calendar.cancel", "cancel all events today")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)
    # run_tool is faked without the confirm gate (gate behavior is covered
    # by the batch tests) — the first reply IS the receipt.
    receipt = await orchestrator.handle_message(chat_id=0, raw_text="cancel all events today")
    assert 'Deleted from your calendar: "Event 0"' in receipt
    assert 'Outcome UNKNOWN for "Event 1"' in receipt and "may have succeeded" in receipt
    assert 'NOT touched' in receipt and '"Event 2"' in receipt
    assert '"Event 1"' not in receipt.split("NOT touched")[1]  # unknown is not listed untouched


async def test_successful_capped_batch_names_the_overflow(monkeypatch):
    """Round-5 P2: after a clean 8-batch the receipt must name the
    remainder — silence reads as done."""
    from kyraan.tools import registry as reg

    monkeypatch.setattr(orchestrator.router, "call", lambda **kw: _FakeRouted(
        text='{"start_iso": "2099-01-02T00:00:00+00:00", "end_iso": "2099-01-02T23:59:59+00:00", "label": "today"}'))
    events = [{"id": f"ev{i}", "title": f"Event {i}", "start": f"2099-01-02T{8+i:02d}:00:00+00:00",
               "end": f"2099-01-02T{9+i:02d}:00:00+00:00", "all_day": False,
               "location": None, "recurring": False} for i in range(11)]

    async def fake_dispatch(spec, args):
        if spec.name == "calendar.list_events":
            return events
        return {"id": args["event_id"], "deleted": True, "already_gone": False}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    _mock_normalize(monkeypatch, "calendar.cancel", "cancel all events today")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    await orchestrator.handle_message(chat_id=0, raw_text="cancel all events today")
    receipt = await orchestrator.handle_message(chat_id=0, raw_text="yes")
    assert "3 event(s) still to cancel" in receipt
    assert '"cancel all events today"' in receipt   # the WINDOW rides along


async def test_window_words_are_never_title_filters(monkeypatch):
    """Round-7 P2: 'cancel all events next month' must sweep the window —
    the extractor's own label words can't double as title filters (this
    exact phrase is what our receipts recommend)."""
    from kyraan.tools import registry as reg

    monkeypatch.setattr(orchestrator.router, "call", lambda **kw: _FakeRouted(
        text='{"start_iso": "2099-02-01T00:00:00+00:00", "end_iso": "2099-02-28T23:59:59+00:00", "label": "next month"}'))
    events = [{"id": "ev1", "title": "Board meeting", "start": "2099-02-10T10:00:00+00:00",
               "end": "2099-02-10T11:00:00+00:00", "all_day": False,
               "location": None, "recurring": False}]

    async def fake_dispatch(spec, args):
        if spec.name == "calendar.list_events":
            return events
        return {"id": args["event_id"], "deleted": True, "already_gone": False}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    _mock_normalize(monkeypatch, "calendar.cancel", "cancel all events next month")

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    ask = await orchestrator.handle_message(chat_id=0, raw_text="cancel all events next month")
    assert "About to DELETE 1 event(s)" in ask and "Board meeting" in ask
    assert "couldn't match" not in ask
