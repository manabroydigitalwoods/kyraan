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
