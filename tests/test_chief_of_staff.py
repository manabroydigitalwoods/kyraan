"""Chief of staff — duty #3 (2026-09-03)."""
import asyncio
from datetime import datetime, timedelta

from kyraan.triggers import chief_of_staff as cos


def _fixed_now(monkeypatch, dt):
    monkeypatch.setattr(cos, "local_now", lambda: dt)


def test_open_mentions_are_those_nobody_answered(monkeypatch):
    from kyraan.triggers import slack_watch
    today = cos.local_now().date().isoformat()
    monkeypatch.setattr(slack_watch, "_owner_user_id", "U1")
    monkeypatch.setattr(slack_watch, "_load", lambda: {
        "kyraan_posted": ["on it"],
        "open_mentions": [
            {"channel": "#social", "ts": "100", "user": "suman", "question": "lunch friday?", "surfaced_at": today},
            {"channel": "#dev", "ts": "200", "user": "titu", "question": "merge?", "surfaced_at": today},
            {"channel": "#dev", "ts": "300", "user": "kamal", "question": "eta?", "surfaced_at": today}]})
    history = {"#social": [{"ts": "150", "user_id": "U1", "text": "sure"}],       # owner answered
               "#dev": [{"ts": "250", "user_id": "U9", "text": "on it"}]}         # Kyraan answered ts 200 only

    async def fake_run(call, **kw):
        return history[call.args["channel_id"]]
    monkeypatch.setattr(cos.kernel, "run_tool", fake_run)
    monkeypatch.setattr(slack_watch, "parse_history", lambda raw: raw)
    got = asyncio.run(cos.open_mentions())
    assert [m["user"] for m in got] == ["kamal"]


def test_still_open_is_silent_when_nothing_is_and_says_it_once(monkeypatch, tmp_path):
    monkeypatch.setattr(cos, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(cos.kernel, "can_send_proactively", lambda **kw: True)
    _fixed_now(monkeypatch, datetime(2026, 9, 3, 18, 0))          # a Thursday
    sent = []

    async def send(chat_id, text):
        sent.append(text); return True

    async def nothing(chat_id): return []
    monkeypatch.setattr(cos, "needs_reply_lines", nothing)
    assert asyncio.run(cos.fire_still_open(1, send)) is False and sent == []

    async def two(chat_id): return ["- Slack #dev: kamal — \"eta?\"", "- Slipped: call mom (was 5:00 PM)"]
    monkeypatch.setattr(cos, "needs_reply_lines", two)
    assert asyncio.run(cos.fire_still_open(1, send)) is True
    assert sent[0].startswith("🗂 Still open today:") and "kamal" in sent[0]
    assert asyncio.run(cos.fire_still_open(1, send)) is False               # same digest: silent
    _fixed_now(monkeypatch, datetime(2026, 9, 5, 18, 0))                  # Saturday: silent
    assert asyncio.run(cos.fire_still_open(1, send)) is False


def test_prep_lines_use_substantive_note_lines(monkeypatch):
    monkeypatch.setattr(cos, "_people_in", lambda title: ["rakesh_chakraborty"] if "rakesh" in title.lower() else [])
    monkeypatch.setattr(cos, "_note_lines", lambda pid, limit=3: ["Close friend since college, runs a print shop", "last photo together: 02 Sep 2026"])
    out = cos.prep_lines([{"title": "Coffee with Rakesh"}, {"title": "Dentist"}])
    assert out[0].startswith("About Rakesh Chakraborty") and len(out) == 3


def test_whats_open_rail(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")

    async def st(chat_id): return "Nothing open — no unanswered Slack mentions, no slipped reminders."
    monkeypatch.setattr(cos, "status_text", st)
    for q in ("what's open", "what needs a reply?", "what did I miss"):
        assert asyncio.run(orchestrator.handle_message(1, q)).startswith("Nothing open"), q
