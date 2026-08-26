"""Nightly self-review (harness pack B): boundary-respecting transcript,
quiet-day skip, DND gate."""
import json

import pytest

from kyraan.control_plane import logging_setup
from kyraan.triggers import self_review


@pytest.fixture
def day_log(monkeypatch, tmp_path):
    log = tmp_path / "chat.jsonl"
    monkeypatch.setattr(logging_setup, "CHAT_LOG", log)
    from kyraan.control_plane.dnd import local_now
    ts = local_now().isoformat()

    def entry(role, text, **extra):
        return json.dumps({"ts": ts, "chat_id": 1, "role": role, "text": text, **extra})

    lines = [entry("user", f"question number {i}") for i in range(7)]
    lines += [entry("assistant", "a fine answer") for _ in range(6)]
    lines.append(entry("assistant",
                       "You have about 9 unread. Latest:\n- Rohan Sen: Secret subject line",
                       cloud_text="[showed the unread email summary]"))
    log.write_text("\n".join(lines))
    return log


async def test_compose_respects_cloud_boundaries(day_log, monkeypatch):
    captured = {}

    class R:
        text = "Reviewed 7 exchanges today. 1 looked wrong.\n- example"

    async def fake_acall(prompt="", system="", **kw):
        captured["prompt"] = prompt
        return R()

    monkeypatch.setattr(self_review.router, "acall", fake_acall)
    report = await self_review.compose(1)
    assert report.startswith("🔍 Daily self-review")
    assert "Secret subject line" not in captured["prompt"]     # redaction honored
    assert "[showed the unread email summary]" in captured["prompt"]


async def test_quiet_day_yields_no_report(monkeypatch, tmp_path):
    log = tmp_path / "chat.jsonl"
    monkeypatch.setattr(logging_setup, "CHAT_LOG", log)
    from kyraan.control_plane.dnd import local_now
    log.write_text(json.dumps({"ts": local_now().isoformat(), "chat_id": 1,
                               "role": "user", "text": "hello"}))

    async def must_not_call(**kw):
        raise AssertionError("no model call on a quiet day")

    monkeypatch.setattr(self_review.router, "acall", must_not_call)
    assert await self_review.compose(1) == ""


async def test_fire_respects_the_proactive_gate(monkeypatch):
    from kyraan.control_plane import kernel

    monkeypatch.setattr(kernel, "can_send_proactively", lambda: False)
    sends = []

    async def send_fn(chat_id, text):
        sends.append(text)

    assert await self_review.fire(1, send_fn) is False
    assert sends == []


# --- the nightly prompt critic (5a) ----------------------------------------

def test_day_signals_digest(monkeypatch, tmp_path):
    import json as _json
    from datetime import datetime, timezone

    from kyraan.control_plane import logging_setup
    from kyraan.triggers import self_review

    now = datetime.now(timezone.utc).isoformat()
    events = [
        {"ts": now, "kind": "agent_deflection_corrected", "draft": "Do you want me to schedule it?"},
        {"ts": now, "kind": "agent_deflection_corrected", "draft": "share a location pin"},
        {"ts": now, "kind": "tool_result", "ok": False, "tool": "web.search", "error": "boom"},
        {"ts": now, "kind": "model_call", "latency_ms": 1200, "input_tokens": 4000, "cached_tokens": 0},
        {"ts": "2020-01-01T00:00:00+00:00", "kind": "tool_loop_detected"},  # not today
    ]
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(_json.dumps(e) for e in events) + "\n")
    monkeypatch.setattr(logging_setup, "EVENT_LOG", p)

    digest = self_review._day_signals()
    assert "agent_deflection_corrected: 2x" in digest
    assert "share a location pin" in digest
    assert "web.search: 1x" in digest
    assert "full cache misses 1/1" in digest
    assert "tool_loop_detected" not in digest  # yesterday's event excluded


async def test_critique_appends_to_review_and_never_sinks_it(monkeypatch):
    from kyraan.triggers import self_review

    monkeypatch.setattr(self_review, "_todays_transcript",
                        lambda chat_id: [("10:00", "user", f"msg {i}") for i in range(8)])

    class _R:
        def __init__(self, text): self.text = text

    calls = []

    async def fake_acall(prompt="", system="", tier="", **kw):
        calls.append(system[:40])
        if "auditing the SYSTEM PROMPT" in system:
            return _R("EDIT: tighten rule X — EVIDENCE: 2 deflections")
        return _R("Reviewed 8 exchanges today. 1 looked wrong.")

    monkeypatch.setattr(self_review.router, "acall", fake_acall)
    monkeypatch.setattr(self_review, "_day_signals", lambda: "GUARD FIRINGS: 2x")
    report = await self_review.compose(chat_id=1)
    assert "Daily self-review" in report
    assert "Prompt critique" in report and "EDIT: tighten rule X" in report
    assert "scripts/eval.py" in report

    # the critic crashing must not sink the review itself
    async def broken(*a, **k):
        raise RuntimeError("critic down")
    monkeypatch.setattr(self_review, "_prompt_critique", broken)
    report = await self_review.compose(chat_id=1)
    assert "Daily self-review" in report and "Prompt critique" not in report
