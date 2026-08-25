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
    assert "permanently\nsaved" in captured["system"] or "permanently saved" in captured["system"]


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

    for i in range(15):  # 15 exchanges = 30 entries > the 20-entry window
        await orchestrator.handle_message(chat_id=1, raw_text=f"message number {i}")

    block = orchestrator._history_block(1)
    assert "message number 0" not in block  # rolled out
    assert "message number 14" in block
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
            {"title": "Standup", "start": "2026-08-25T09:30:00+05:30", "end": "2026-08-25T09:45:00+05:30", "all_day": False, "location": None},
            {"title": "Holiday", "start": "2026-08-25T00:00:00+05:30", "end": "2026-08-26T00:00:00+05:30", "all_day": True, "location": None},
        ]

    monkeypatch.setattr(orchestrator.kernel, "run_tool", fake_run_tool)

    result = await orchestrator.handle_message(chat_id=0, raw_text="what's on my calendar today")
    assert "Calendar today:" in result
    assert "09:30 — Standup" in result
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


async def test_qa_prompt_forbids_pretending_to_create_calendar_events(monkeypatch):
    """Found live: asked to add a calendar event (a write Kyraan can't do
    yet), it played along, then silently created a reminder while the user
    believed it was a calendar event. The prompt must force the honest
    answer: can't write to the calendar, offer a reminder instead."""
    _mock_normalize(monkeypatch, "qa.answer")
    captured = {}

    def fake_call(prompt, system="", **kwargs):
        captured["system"] = system
        return _FakeRouted(text="I can't create calendar events yet — want a reminder instead?")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)

    await orchestrator.handle_message(chat_id=0, raw_text="can you set an event in my calendar")
    assert "CANNOT create, edit, or delete calendar" in captured["system"]
    assert "never present a reminder as a calendar event" in captured["system"]
