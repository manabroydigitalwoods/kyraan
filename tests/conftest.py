"""Shared test fixtures.

The audit log is a first-class safety feature — "trace why" means reading
logs/events.jsonl — so test runs must never write into it. Before this
fixture, every pytest run sprayed its tool_call/model_call events into the
production file, which made forensics on real sessions (like the 15:00
double-send investigation) measurably harder.
"""
import os

import pytest

# The suite must pass on ANY machine in ANY timezone (CI runs in UTC, the
# dev laptop in IST, a reviewer's box wherever). Pin the app's zone before
# any kyraan import can read it: tests that assert wall-clock behavior get
# one deterministic zone, and an ambient KYRAAN_TIMEZONE from a dev .env
# can never leak in. Host-TZ independence is separate — nothing in code
# may use naive datetimes or the system zone (CI proves it by running the
# suite under TZ=America/New_York).
os.environ["KYRAAN_TIMEZONE"] = "UTC"

from kyraan.control_plane import logging_setup  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_event_log(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_setup, "EVENT_LOG", tmp_path / "events.jsonl")
    monkeypatch.setattr(logging_setup, "CHAT_LOG", tmp_path / "chat.jsonl")
    monkeypatch.setattr(logging_setup, "TRACE_LOG", tmp_path / "traces.jsonl")
    monkeypatch.setattr(logging_setup, "ARCHIVE_DIR", tmp_path / "log_archive")


@pytest.fixture(autouse=True)
def _no_fact_mirroring(monkeypatch, tmp_path):
    """The memory tree is isolated below, but the P3.2a/P3.2d mirrors
    would still write into the LIVE Postgres container — off by default;
    the pg-marked sync tests re-enable them and clean up their own rows."""
    from kyraan.store import facts, promises, sync_state, triples
    monkeypatch.setattr(facts, "MIRROR_ENABLED", False)
    monkeypatch.setattr(promises, "MIRROR_ENABLED", False)
    monkeypatch.setattr(triples, "EXTRACT_ENABLED", False)
    # sync_state persists to data/pg_sync_state.json — a pg-down TEST
    # wrote a real stale mark and live reads then refused PG for hours
    # (found live 2026-08-27: 73 fallback events, all test pollution).
    monkeypatch.setattr(sync_state, "STATE_PATH",
                        tmp_path / "pg_sync_state.json")
    # Post-cutover the CODE default is pg — unit tests pin files
    # explicitly (delenv alone would now mean pg).
    monkeypatch.setenv("KYRAAN_MEMORY_BACKEND", "files")
    monkeypatch.setenv("KYRAAN_PROMISES_BACKEND", "files")


@pytest.fixture(autouse=True)
def _isolated_memory_tree(monkeypatch, tmp_path):
    """No test may ever touch the REAL memory tree — a promote test once
    leaked "likes tea" into the owner's live index.json. Tests that need
    their own layout still re-patch on top of this."""
    from kyraan.memory import store as memory_store
    root = tmp_path / "memory_root"
    (root / "pending_review").mkdir(parents=True)
    monkeypatch.setattr(memory_store, "MEMORY_ROOT", root)
    monkeypatch.setattr(memory_store, "PENDING_DIR", root / "pending_review")


@pytest.fixture(autouse=True)
def _isolated_session_summaries(monkeypatch, tmp_path):
    """Summary rolls in tests must never write the real data/ file."""
    from kyraan.agents import session
    monkeypatch.setattr(session, "_summaries_path",
                        lambda: tmp_path / "session_summaries.json")


@pytest.fixture(autouse=True)
def _isolated_data_stores(monkeypatch, tmp_path):
    """No test may write the production data/ stores. Found live
    (2026-08-26): ~20 chat-0/91/92 reminder records from past test runs
    sat in the real reminders.json, and the bot logged "Retiring reminder
    for non-owner chat" at every boot. Tests that need their own paths
    still re-patch on top of this."""
    from kyraan.model_router import router
    from kyraan.triggers import agent_tasks
    from kyraan.triggers import store as reminder_store
    from kyraan.memory import review_scaling
    monkeypatch.setattr(review_scaling, "STATS_PATH",
                        tmp_path / "review_stats.json")
    from kyraan.triggers import health_alerts
    monkeypatch.setattr(health_alerts, "STATE_PATH",
                        tmp_path / "health_alerts.json")
    monkeypatch.setattr(health_alerts, "_recent", {})
    from kyraan.triggers import event_rules
    monkeypatch.setattr(event_rules, "RULES_PATH",
                        tmp_path / "event_rules.json")
    from kyraan.triggers import curiosity
    monkeypatch.setattr(curiosity, "STATE_PATH",
                        tmp_path / "curiosity.json")
    monkeypatch.setattr(reminder_store, "REMINDERS_PATH", tmp_path / "reminders.json")
    monkeypatch.setattr(agent_tasks, "TASKS_PATH", tmp_path / "agent_tasks.json")
    monkeypatch.setattr(router, "COST_LEDGER_PATH", tmp_path / "cost_ledger.json")
    from kyraan.agents import faces
    monkeypatch.setattr(faces, "FACES_DIR", tmp_path / "faces")
    # The PG sync-state file is shared by four stores AND written by the
    # live bot on this same machine — an unisolated test run contends
    # with it over a real file lock. Same rule as every other store.
    from kyraan.store import sync_state
    monkeypatch.setattr(sync_state, "STATE_PATH", tmp_path / "pg_sync_state.json")
