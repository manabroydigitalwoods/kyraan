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
    def fake_normalize(raw_text, tier="cheap"):
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


async def test_qa_system_prompt_forbids_claiming_to_save_memories(monkeypatch):
    """Found live (2026-08-25): "remember that my wife's name is Mira" got
    "Got it—I've noted that" back, while nothing was written anywhere —
    extraction isn't wired up yet. Until it is, the qa.answer system prompt
    must tell the model it has no memory, so it can't falsely claim a fact
    was saved."""
    _mock_normalize(monkeypatch, "qa.answer")
    captured = {}

    def fake_call(prompt, system="", **kwargs):
        captured["system"] = system
        return _FakeRouted(text="I can't store memories yet.")

    monkeypatch.setattr(orchestrator.router, "call", fake_call)

    await orchestrator.handle_message(chat_id=0, raw_text="remember that my wife's name is Mira")
    assert "cannot save facts" in captured["system"]


@pytest.fixture(autouse=True)
def _clear_pending_confirmations():
    """_pending_confirmations is module-level state — never let one test's
    stashed confirmation leak into another."""
    orchestrator._pending_confirmations.clear()
    yield
    orchestrator._pending_confirmations.clear()


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
