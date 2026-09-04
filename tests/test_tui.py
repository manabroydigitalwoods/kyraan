"""Headless tests for scripts/tui.py using Textual's Pilot harness — no real
terminal or live model call needed. orchestrator.handle_message is mocked
so this stays fast and deterministic, same rationale as test_normalize.py.
"""
import sys
from pathlib import Path

import pytest

import asyncio  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tui as tui_module  # noqa: E402
from kyraan.control_plane import config, kill_switch  # noqa: E402
from kyraan.model_router import router  # noqa: E402
from textual.widgets import Collapsible, LoadingIndicator  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_kill_switch():
    kill_switch.disengage()
    yield
    kill_switch.disengage()


@pytest.fixture(autouse=True)
def _clean_tier_overrides():
    config.clear_tier_overrides()
    yield
    config.clear_tier_overrides()


async def test_sending_a_message_updates_sidebar_and_count(monkeypatch):
    async def fake_handle_message(chat_id, text):
        return "mocked reply"

    monkeypatch.setattr(tui_module.orchestrator, "handle_message", fake_handle_message)
    monkeypatch.setattr(
        router,
        "last_call",
        router.RoutedResponse(
            text="mocked reply",
            tier_used="cheap",
            provider="ollama",
            model="llama3.2",
            latency_ms=42.0,
            usage=router.Usage(input_tokens=10, output_tokens=5),
        ),
    )

    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"hi")
        await pilot.press("enter")
        await pilot.pause()

        assert app.message_count == 1
        assert app.total_input_tokens == 10
        assert app.total_output_tokens == 5


async def test_kill_and_unkill_slash_commands_toggle_the_real_kill_switch():
    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"/kill")
        await pilot.press("enter")
        await pilot.pause()
        assert kill_switch.is_engaged()

        await pilot.press(*"/unkill")
        await pilot.press("enter")
        await pilot.pause()
        assert not kill_switch.is_engaged()


async def test_input_is_focused_on_launch():
    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "chat-input"


async def test_show_thinking_mounts_a_loading_indicator():
    """Verified live via screenshots that the indicator is visible for the
    duration of a real (slow) call. Pilot's message-pump timing in this
    harness doesn't let a test reliably observe "mid-flight" state — by the
    time control returns to the test body, queued async work (including a
    mocked call's own artificial delay) has often already been drained past
    that point — so this checks the mounting mechanism directly instead of
    racing real elapsed time."""
    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        indicator = await app._show_thinking()
        assert len(app.query(LoadingIndicator)) == 1
        await indicator.remove()
        await pilot.pause()
        assert len(app.query(LoadingIndicator)) == 0


async def test_no_loading_indicator_left_over_after_a_completed_exchange(monkeypatch):
    async def fake_handle_message(chat_id, text):
        return "mocked reply"

    monkeypatch.setattr(tui_module.orchestrator, "handle_message", fake_handle_message)

    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"hi")
        await pilot.press("enter")
        await pilot.pause()
        assert len(app.query(LoadingIndicator)) == 0


async def test_reasoning_shows_as_a_collapsed_thought_section(monkeypatch):
    async def fake_handle_message(chat_id, text):
        return "mocked reply"

    monkeypatch.setattr(tui_module.orchestrator, "handle_message", fake_handle_message)
    monkeypatch.setattr(
        router,
        "last_call",
        router.RoutedResponse(
            text="mocked reply",
            tier_used="frontier",
            provider="groq",
            model="openai/gpt-oss-120b",
            latency_ms=500.0,
            usage=router.Usage(input_tokens=20, output_tokens=15),
            reasoning="step by step hidden reasoning",
        ),
    )

    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"hi")
        await pilot.press("enter")
        await pilot.pause()

        thoughts = app.query(Collapsible)
        assert len(thoughts) == 1
        assert thoughts.first().collapsed is True
        assert thoughts.first().title.startswith("Thought · 500ms")


async def test_no_reasoning_means_no_thought_section(monkeypatch):
    async def fake_handle_message(chat_id, text):
        return "mocked reply"

    monkeypatch.setattr(tui_module.orchestrator, "handle_message", fake_handle_message)
    monkeypatch.setattr(
        router,
        "last_call",
        router.RoutedResponse(
            text="mocked reply",
            tier_used="cheap",
            provider="ollama",
            model="llama3.2",
            latency_ms=42.0,
            usage=router.Usage(input_tokens=10, output_tokens=5),
        ),
    )

    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"hi")
        await pilot.press("enter")
        await pilot.pause()

        assert len(app.query(Collapsible)) == 0


async def test_reminders_command_does_not_call_the_model(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("orchestrator.handle_message should not be called for /reminders")

    monkeypatch.setattr(tui_module.orchestrator, "handle_message", fail_if_called)

    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"/reminders")
        await pilot.press("enter")
        await pilot.pause()
        # no exception means fail_if_called was never invoked


async def test_retry_resends_the_last_message(monkeypatch):
    calls = []

    async def fake_handle_message(chat_id, text):
        calls.append(text)
        return "mocked reply"

    monkeypatch.setattr(tui_module.orchestrator, "handle_message", fake_handle_message)

    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"hi there")
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press(*"/retry")
        await pilot.press("enter")
        await pilot.pause()

        assert calls == ["hi there", "hi there"]
        assert app.message_count == 2


async def test_retry_with_nothing_sent_yet_does_not_call_the_model(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("orchestrator.handle_message should not be called")

    monkeypatch.setattr(tui_module.orchestrator, "handle_message", fail_if_called)

    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"/retry")
        await pilot.press("enter")
        await pilot.pause()
        # no exception means fail_if_called was never invoked


async def test_tier_command_with_no_args_shows_current_config():
    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"/tier")
        await pilot.press("enter")
        await pilot.pause()
        # doesn't raise; config.load() still reflects the real file
        assert "cheap" in config.load()["model_tiers"]


async def test_tier_command_overrides_a_tier_for_this_session():
    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"/tier cheap groq openai/gpt-oss-20b")
        await pilot.press("enter")
        await pilot.pause()

        assert config.load()["model_tiers"]["cheap"] == {"provider": "groq", "model": "openai/gpt-oss-20b"}


async def test_tier_command_rejects_an_unknown_provider():
    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"/tier cheap not-a-real-provider some-model")
        await pilot.press("enter")
        await pilot.pause()

        # unchanged — override was rejected
        assert config.load()["model_tiers"]["cheap"]["provider"] == "openai"   # local text models retired 2026-09-04


async def test_export_writes_a_transcript_file(monkeypatch, tmp_path):
    async def fake_handle_message(chat_id, text):
        return "mocked reply"

    monkeypatch.setattr(tui_module.orchestrator, "handle_message", fake_handle_message)
    monkeypatch.setattr(tui_module, "TRANSCRIPT_DIR", tmp_path)

    app = tui_module.KyraanTUI()
    async with app.run_test() as pilot:
        await pilot.press(*"hi")
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press(*"/export")
        await pilot.press("enter")
        await pilot.pause()

    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "hi" in content
    assert "mocked reply" in content
