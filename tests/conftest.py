"""Shared test fixtures.

The audit log is a first-class safety feature — "trace why" means reading
logs/events.jsonl — so test runs must never write into it. Before this
fixture, every pytest run sprayed its tool_call/model_call events into the
production file, which made forensics on real sessions (like the 15:00
double-send investigation) measurably harder.
"""
import pytest

from kyraan.control_plane import logging_setup


@pytest.fixture(autouse=True)
def _isolated_event_log(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_setup, "EVENT_LOG", tmp_path / "events.jsonl")
    monkeypatch.setattr(logging_setup, "CHAT_LOG", tmp_path / "chat.jsonl")
    monkeypatch.setattr(logging_setup, "TRACE_LOG", tmp_path / "traces.jsonl")


@pytest.fixture(autouse=True)
def _classifier_path_by_default(monkeypatch):
    """Legacy orchestrator tests exercise the classifier path; the agent
    loop (which would consume their scripted router fakes first) is opt-in
    per test via monkeypatch.setattr(orchestrator, "AGENT_LOOP_ENABLED", True)."""
    from kyraan.agents import orchestrator
    monkeypatch.setattr(orchestrator, "AGENT_LOOP_ENABLED", False)


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
    monkeypatch.setattr(reminder_store, "REMINDERS_PATH", tmp_path / "reminders.json")
    monkeypatch.setattr(agent_tasks, "TASKS_PATH", tmp_path / "agent_tasks.json")
    monkeypatch.setattr(router, "COST_LEDGER_PATH", tmp_path / "cost_ledger.json")
    from kyraan.agents import faces
    monkeypatch.setattr(faces, "FACES_DIR", tmp_path / "faces")
