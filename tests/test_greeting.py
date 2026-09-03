"""A greeting gets a greeting and one glance (2026-09-04)."""
import asyncio
from datetime import datetime


def test_greeting_is_time_of_day_plus_a_glance(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    from kyraan.triggers import chief_of_staff
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    monkeypatch.setattr(orchestrator, "local_now", lambda: datetime(2026, 9, 4, 21, 12))

    async def fake_run(call, **kw):
        return [{"title": "Call Rakesh", "start": "2026-09-04T22:00:00+05:30", "all_day": False}]
    monkeypatch.setattr(kernel, "run_tool", fake_run)

    async def waiting(chat_id): return ["- Slack #dev: kamal — \"eta?\""]
    monkeypatch.setattr(chief_of_staff, "needs_reply_lines", waiting)
    out = asyncio.run(orchestrator.handle_message(1, "hello"))
    assert out.startswith("Evening. Next: Call Rakesh at") and "1 thing waiting" in out
    assert "Maan" not in out and "?" not in out.split("say")[0]

    async def nothing(chat_id): return []
    monkeypatch.setattr(chief_of_staff, "needs_reply_lines", nothing)

    async def no_events(call, **kw): return []
    monkeypatch.setattr(kernel, "run_tool", no_events)
    assert asyncio.run(orchestrator.handle_message(1, "hey")) == "Evening. Nothing waiting."


def test_voice_drops_the_servant_question():
    from kyraan.agents.agent_loop import voice
    assert voice("Hello, Maan. What do you want done?") == "Hello, Maan."
    assert voice("The AC is on. Anything else you need?") == "The AC is on."
    assert voice("What do you want done?") == "What do you want done?"          # not emptied
