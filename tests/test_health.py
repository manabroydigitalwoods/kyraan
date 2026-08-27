"""The health layer (2026-08-27): per-turn anomaly collection, the
throttled warning lights, and the doctor's census + verdict."""
import json

import pytest

from kyraan.agents import agent_loop, orchestrator
from kyraan.control_plane import health, logging_setup
from kyraan.triggers import health_alerts


# --- per-turn collection --------------------------------------------------

async def test_turn_health_records_the_anomalies(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(orchestrator, "log_event",
                        lambda kind, **kw: events.append((kind, kw)))

    async def fake_run(chat_id, raw_text, tier):
        logging_setup.log_event("model_call_error", tier=tier, error="boom")
        logging_setup.log_event("agent_tier_fallback", tier=tier)
        return "ok."

    monkeypatch.setattr(agent_loop, "run", fake_run)
    await orchestrator.handle_message(960_001, "what's the weather like today?")
    kind, payload = next((k, p) for k, p in events if k == "turn_health")
    assert payload["anomalies"] == ["agent_tier_fallback", "model_call_error"]
    assert payload["anomaly_count"] == 2
    assert payload["latency_ms"] >= 0


async def test_clean_turn_reports_no_anomalies(monkeypatch):
    events = []
    monkeypatch.setattr(orchestrator, "log_event",
                        lambda kind, **kw: events.append((kind, kw)))

    async def fake_run(chat_id, raw_text, tier):
        return "fine."

    monkeypatch.setattr(agent_loop, "run", fake_run)
    await orchestrator.handle_message(960_002, "hi")  # short: no extraction
    _, payload = next((k, p) for k, p in events if k == "turn_health")
    assert payload["anomalies"] is None and payload["anomaly_count"] == 0


# --- the warning lights ---------------------------------------------------

def test_critical_alerts_on_first_sight_then_throttles():
    first = health_alerts.check(["agent_all_tiers_failed"])
    assert first and "unreachable" in first
    assert health_alerts.check(["agent_all_tiers_failed"]) is None  # today: once


def test_rate_kind_needs_a_burst():
    assert health_alerts.check(["model_call_error"]) is None
    assert health_alerts.check(["model_call_error"]) is None
    third = health_alerts.check(["model_call_error"])
    assert third and "keeps erroring" in third


def test_unlisted_kinds_never_alert():
    assert health_alerts.check(["episode_rag_skipped"]) is None


async def test_alert_rides_the_reply_in_band(monkeypatch):
    async def fake_run(chat_id, raw_text, tier):
        logging_setup.log_event("agent_all_tiers_failed")
        return "degraded answer."

    monkeypatch.setattr(agent_loop, "run", fake_run)
    reply = await orchestrator.handle_message(960_003, "anything at all here")
    assert "⚠️ Health:" in reply and "health report" in reply


# --- the doctor -----------------------------------------------------------

def _write_events(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_census_counts_and_verdict(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=30)).isoformat()
    fresh = now.isoformat()
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"ts": fresh, "kind": "turn_health", "anomaly_count": 0},
        {"ts": fresh, "kind": "turn_health", "anomaly_count": 2},
        *[{"ts": fresh, "kind": "model_call_error"} for _ in range(6)],
        {"ts": old, "kind": "handle_message_error"},   # outside 24h
    ])
    monkeypatch.setattr(logging_setup, "EVENT_LOG", log)
    monkeypatch.setattr(logging_setup, "ARCHIVE_DIR", tmp_path / "none")
    monkeypatch.setattr(health, "_probe_components",
                        lambda: [("postgres", "OK", "up")])
    verdict, text = health.report()
    assert verdict == "WARN"                    # 6 model errors >= 5
    assert "model_call_error ×6" in text
    assert "handle_message_error" not in text   # stale event ignored
    assert "2 turns, 1 with anomalies" in text


def test_component_failure_is_a_fail_verdict(monkeypatch, tmp_path):
    log = tmp_path / "events.jsonl"
    log.write_text("")
    monkeypatch.setattr(logging_setup, "EVENT_LOG", log)
    monkeypatch.setattr(logging_setup, "ARCHIVE_DIR", tmp_path / "none")
    monkeypatch.setattr(health, "_probe_components",
                        lambda: [("redis", "FAIL", "connection refused")])
    verdict, text = health.report()
    assert verdict == "FAIL"
    assert "redis is down" in text


async def test_health_report_chat_phrase(monkeypatch):
    monkeypatch.setattr(health, "report", lambda: ("OK", "COMPONENTS: all up"))
    reply = await orchestrator._dispatch(960_004, "health report")
    assert reply.startswith("🩺 OK")
    assert "COMPONENTS" in reply

async def test_non_owner_turns_never_carry_the_warning_line(monkeypatch):
    from kyraan.control_plane import kernel

    async def fake_run(chat_id, raw_text, tier):
        logging_setup.log_event("agent_all_tiers_failed")
        return "her reply."

    monkeypatch.setattr(agent_loop, "run", fake_run)
    token = kernel.set_viewer("ruma", "read_mostly")
    try:
        reply = await orchestrator.handle_message(960_005, "hello out there!")
    finally:
        kernel.reset_viewer_stage(token)
    assert "⚠️ Health" not in reply
    # and the daily alert was NOT burned — the owner still gets it
    assert health_alerts.check(["agent_all_tiers_failed"]) is not None


async def test_health_report_phrase_is_owner_only(monkeypatch):
    from kyraan.control_plane import kernel
    monkeypatch.setattr(health, "report",
                        lambda: ("OK", "SECRET INTERNALS"))

    async def fake_run(chat_id, raw_text, tier):
        return "normal reply."

    monkeypatch.setattr(agent_loop, "run", fake_run)
    token = kernel.set_viewer("ruma", "read_mostly")
    try:
        reply = await orchestrator._dispatch(960_006, "health report")
    finally:
        kernel.reset_viewer_stage(token)
    assert "SECRET INTERNALS" not in reply
