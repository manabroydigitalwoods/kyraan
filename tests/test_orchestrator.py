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
        lambda **kwargs: _FakeRouted(text='{"text": "test", "when_iso": "2026-08-25T13:26:44+05:30+04:00"}'),
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

    async def fake_propose(raw_text):
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

    async def fake_propose(raw_text):
        return ["- Wife's name is Mira"]

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", fake_propose)

    result = await orchestrator.handle_message(chat_id=0, raw_text="my wife's name is Mira")
    assert result.startswith("Nice!")
    assert "Noted for review: Wife's name is Mira" in result


async def test_extraction_failure_never_breaks_the_reply(monkeypatch):
    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="Nice!"))

    async def broken_propose(raw_text):
        raise RuntimeError("extraction blew up")

    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", broken_propose)

    result = await orchestrator.handle_message(chat_id=0, raw_text="my wife's name is Mira")
    assert result == "Nice!"


async def test_short_messages_skip_extraction_entirely(monkeypatch):
    _mock_normalize(monkeypatch, "qa.answer")
    monkeypatch.setattr(orchestrator.router, "call", lambda **kwargs: _FakeRouted(text="Hey!"))
    calls = []

    async def counting_propose(raw_text):
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


async def test_email_check_formats_metadata_and_redacts_history(monkeypatch):
    """The §3a data boundary: the user sees senders/subjects, but the
    conversation history — which feeds the cloud classifier and qa
    prompts — records only a placeholder."""
    _mock_normalize(monkeypatch, "email.check")

    async def fake_run_tool(call, **kwargs):
        assert call.tool_name == "email.unread"
        return {"unread_estimate": 3, "messages": [
            {"from": '"Suman Das" <s@x.com>', "subject": "Invoice pending", "date": "d"},
            {"from": "noreply@bank.com", "subject": "Statement", "date": "d"},
        ]}

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)

    result = await orchestrator.handle_message(chat_id=0, raw_text="any new emails?")
    assert "about 3 unread" in result
    assert "Suman Das: Invoice pending" in result
    assert "noreply@bank.com: Statement" in result

    history = orchestrator._history_block(0)
    assert "Invoice pending" not in history and "Suman Das" not in history
    assert "[showed the unread email summary]" in history


async def test_email_failure_surfaces_and_history_stays_clean(monkeypatch):
    _mock_normalize(monkeypatch, "email.check")

    async def failing_run_tool(call, **kwargs):
        raise orchestrator.kernel.ToolFailed("email.unread failed: re-run setup_google_oauth")

    monkeypatch.setattr(orchestrator.kernel, "run_tool", failing_run_tool)
    result = await orchestrator.handle_message(chat_id=0, raw_text="any new emails?")
    assert "Couldn't check email" in result


async def test_unconverged_switch_reply_is_honest(monkeypatch):
    _mock_normalize(monkeypatch, "home.control", "turn on the AC")

    async def fake_run_tool(call, **kwargs):
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

    async def counting(raw_text):
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
