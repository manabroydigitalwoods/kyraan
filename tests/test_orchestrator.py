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
