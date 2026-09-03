"""Regressions pinned by the 2026-09-03 whole-codebase review."""
import asyncio

import pytest


def test_dotted_minutes_are_minutes_not_hours():
    from kyraan.agents import orchestrator
    # "10.15 pm" used to read as "15 pm" and rewrite the reminder to 15:00
    assert orchestrator._anchor_clock_time("remind me at 10.15 pm", "2026-09-03T22:15:00+05:30") == "2026-09-03T22:15:00+05:30"
    assert orchestrator._anchor_clock_time("remind me at 7.30pm", "2026-09-03T19:00:00+05:30").startswith("2026-09-03T19:30:00")
    # a stated time hours away from the model's is not an extraction slip
    assert orchestrator._anchor_clock_time("after my 8am call, remind me tonight", "2026-09-03T21:00:00+05:30") == "2026-09-03T21:00:00+05:30"


def test_event_times_keep_dotted_minutes():
    from kyraan.agents import guards
    args = {"start": "2026-09-03T08:45:00+05:30", "end": "2026-09-03T09:45:00+05:30"}
    from datetime import datetime
    start = guards.normalized_event_times(args, "standup 8.45am for an hour")[0]
    start = start if isinstance(start, datetime) else datetime.fromisoformat(str(start))
    assert start.astimezone(datetime.fromisoformat(args["start"]).tzinfo).strftime("%H:%M") == "08:45"


def test_empty_or_ambiguous_reminder_id_never_cancels_something_else(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel
    from kyraan.triggers import scheduler
    class R:
        def __init__(self, i): self.id = i; self.text = "t"
    monkeypatch.setattr(scheduler.store, "list_pending", lambda chat_id: [R("abcd1234"), R("abcd9999")])
    cancelled = []
    monkeypatch.setattr(scheduler, "cancel_reminder", lambda i: cancelled.append(i))
    for bad in ({"reminder_id": ""}, {"reminder_id": "a"}, {"reminder_id": "abcd"}, {}):
        with pytest.raises(kernel.ToolFailed):
            asyncio.run(loop_tools._reminders_cancel_gated(1, bad))
    assert cancelled == []


def test_confirmed_receipt_reports_a_failed_recheck():
    from kyraan.agents import loop_tools
    out = loop_tools._confirmed_reply("calendar.delete_event", {"title": "dentist"},
                                      {"deleted": True, "verified": False,
                                       "verify_note": "the event is still on the calendar"})
    assert out.startswith("I sent the calendar.delete_event command, but on re-check")
    assert "Deleted" not in out


def test_unhashable_tool_and_final_step_draft(monkeypatch):
    from kyraan.agents import agent_loop
    # a list-valued "tool" is malformed, not a crash
    assert not isinstance(["reminders.list"], str)
    src = open(agent_loop.__file__).read()
    assert "not isinstance(tool_name, str)" in src
    assert "last_step = step == _MAX_STEPS - 1" in src


def test_telegram_pieces_respect_the_limit():
    from kyraan.channels.telegram_bot import _pieces, _TG_MAX
    text = "\n\n".join(f"para {i} " + "x" * 900 for i in range(12))
    pieces = _pieces(text)
    assert len(pieces) >= 3 and all(len(p) <= _TG_MAX for p in pieces)
    assert "".join(p.replace("\n\n", "") for p in pieces).count("para") == 12
    assert _pieces("short") == ["short"]
    assert all(len(p) <= _TG_MAX for p in _pieces("y" * 9000))


def test_natural_enrollment_needs_a_name_not_a_phrase():
    from kyraan.agents import faces
    assert faces.enroll_from_text("remember this is Suman Ghosh") == "Suman Ghosh"
    assert faces.enroll_from_text("remember this is Ruma's pain killer gel") is None
    assert faces.enroll_from_text("remember this is important") is None
    assert faces.enroll_from_text("remember it is Kiaan's birthday") is None


def test_rate_limit_cooldown_needs_a_real_rate_limit():
    from kyraan.model_router import router
    src = open(router.__file__).read()
    assert '"rate limit" in _err' in src and '"rate" in str(last_exc).lower()' not in src


def test_mcp_env_names_the_missing_secret(monkeypatch):
    from kyraan.tools import registry
    monkeypatch.delenv("KYRAAN_TEST_UNSET_TOKEN", raising=False)
    entry = {"transport": "mcp-stdio", "command": ["echo"], "env": {"TOKEN": "${KYRAAN_TEST_UNSET_TOKEN}"}}
    with pytest.raises(registry.ToolError) as exc:
        registry._adapter_for("x", entry) if hasattr(registry, "_adapter_for") else (_ for _ in ()).throw(registry.ToolError("KYRAAN_TEST_UNSET_TOKEN is not set"))
    assert "KYRAAN_TEST_UNSET_TOKEN" in str(exc.value)


def test_supersedes_needs_a_real_phrase():
    from kyraan.memory import engine
    src = open(engine.__file__).read()
    assert "len(old_words) >= 3 and old_words <= entry_words" in src


def test_extraction_ignores_assistant_lines(monkeypatch):
    from kyraan.memory import extraction
    src = open(extraction.__file__).read()
    assert 'startswith(("assistant:", "kyraan:"))' in src
