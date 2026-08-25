"""Headless tests for scripts/tui.py using Textual's Pilot harness — no real
terminal or live model call needed. orchestrator.handle_message is mocked
so this stays fast and deterministic, same rationale as test_normalize.py.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tui as tui_module  # noqa: E402
from kyraan.control_plane import kill_switch  # noqa: E402
from kyraan.model_router import router  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_kill_switch():
    kill_switch.disengage()
    yield
    kill_switch.disengage()


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
