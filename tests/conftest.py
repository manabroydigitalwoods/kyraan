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
