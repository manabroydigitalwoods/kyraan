"""The CLI channel (§3d #5): conversation-only by construction, owner
viewer, and the crash-contained REPL turn."""
import pytest

from kyraan.channels import cli


def test_no_proactive_wiring_in_the_cli():
    """The dev-harness lesson as a pinned property: this channel must
    never initialize a scheduler — the Telegram process owns every
    proactive job, so a CLI session can't steal a reminder."""
    import inspect
    src = inspect.getsource(cli)
    for forbidden in ("scheduler.init", "agent_tasks.init", "goals.init",
                      "_wire_", "run_repeating", "run_daily", "run_once"):
        assert forbidden not in src, f"CLI wires proactive surface: {forbidden}"


def test_owner_chat_fails_closed(monkeypatch):
    monkeypatch.delenv("TELEGRAM_OWNER_ID", raising=False)
    with pytest.raises(SystemExit, match="TELEGRAM_OWNER_ID"):
        cli._owner_chat()
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "notanumber")
    with pytest.raises(SystemExit):
        cli._owner_chat()
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "424242")
    assert cli._owner_chat() == 424242


async def test_repl_dispatches_and_survives_a_crashing_turn(monkeypatch, capsys):
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "424242")
    inputs = iter(["hello", "boom", ""])  # then EOF
    seen = []

    async def fake_handle(chat_id, text):
        seen.append((chat_id, text, kernel.viewer_person(),
                     kernel.viewer_stage()))
        if text == "boom":
            raise RuntimeError("kaput")
        return "Hi Maan."

    monkeypatch.setattr(orchestrator, "handle_message", fake_handle)

    import builtins

    def fake_input(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, "input", fake_input)
    await cli.repl()
    out = capsys.readouterr().out
    assert "Hi Maan." in out
    assert "turn failed: kaput" in out          # crash contained, REPL lived
    assert seen[0] == (424242, "hello", "owner", "owner")
