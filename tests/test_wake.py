"""Wake scheduling (§3d #4): the planner arms ONE pmset wake ahead of
the earliest due moment, re-arms only when the target moves, and stays
quiet-but-honest when sudo isn't configured."""
from datetime import datetime, time, timedelta, timezone

import pytest

from kyraan.control_plane import wake

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 8, 31, 22, 0, tzinfo=IST)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(wake, "_last_armed", None)
    monkeypatch.setattr(wake, "_sudo_warned", False)
    # empty every source unless a test fills it
    from kyraan.triggers import agent_tasks, goals, store
    monkeypatch.setattr(store, "list_pending", lambda chat=None: [])
    monkeypatch.setattr(agent_tasks, "list_active", lambda chat=None: [])
    monkeypatch.setattr(goals, "_load", lambda: [])
    from kyraan.triggers import briefs, self_review
    monkeypatch.setattr(briefs, "brief_time", lambda which="morning": None)
    monkeypatch.setattr(self_review, "review_time", lambda: None)


class _R:
    def __init__(self, iso):
        self.when_iso = iso


def test_next_due_picks_the_earliest_across_sources(monkeypatch):
    from kyraan.triggers import agent_tasks, store
    monkeypatch.setattr(store, "list_pending", lambda chat=None: [
        _R("2026-08-31T23:30:00+05:30"), _R("2026-09-01T10:00:00+05:30")])
    monkeypatch.setattr(agent_tasks, "list_active", lambda chat=None: [
        _R("2026-08-31T23:00:00+05:30")])
    assert wake.next_due_time(NOW).isoformat() == "2026-08-31T23:00:00+05:30"


def test_daily_brief_times_roll_to_tomorrow(monkeypatch):
    from kyraan.triggers import briefs
    monkeypatch.setattr(briefs, "brief_time",
                        lambda which="morning": time(7, 30)
                        if which == "morning" else None)
    due = wake.next_due_time(NOW)
    assert due.isoformat() == "2026-09-01T07:30:00+05:30"  # 22:00 -> tomorrow


def test_past_and_beyond_horizon_dues_are_ignored(monkeypatch):
    from kyraan.triggers import store
    monkeypatch.setattr(store, "list_pending", lambda chat=None: [
        _R("2026-08-31T09:00:00+05:30"),      # past
        _R("2026-09-02T22:00:00+05:30")])     # beyond 20h horizon
    assert wake.next_due_time(NOW) is None


def test_plan_arms_once_and_rearms_only_on_change(monkeypatch):
    from kyraan.triggers import store
    calls = []
    monkeypatch.setattr(wake, "_pmset", lambda args: calls.append(args) or True)
    monkeypatch.setattr(store, "list_pending", lambda chat=None: [
        _R("2026-08-31T23:00:00+05:30")])
    assert wake.plan(NOW) == "08/31/26 22:58:00"   # 2-min buffer
    assert wake.plan(NOW) == "08/31/26 22:58:00"   # unchanged target
    assert len(calls) == 1                          # no duplicate pmset
    monkeypatch.setattr(store, "list_pending", lambda chat=None: [
        _R("2026-08-31T22:40:00+05:30")])
    assert wake.plan(NOW) == "08/31/26 22:38:00"
    assert len(calls) == 2


def test_imminent_due_needs_no_wake(monkeypatch):
    from kyraan.triggers import store
    monkeypatch.setattr(wake, "_pmset", lambda args: True)
    monkeypatch.setattr(store, "list_pending", lambda chat=None: [
        _R("2026-08-31T22:01:00+05:30")])      # inside the buffer: awake now
    assert wake.plan(NOW) is None


def test_sudo_unavailable_logs_once_and_degrades(monkeypatch):
    from kyraan.triggers import store
    monkeypatch.setattr(store, "list_pending", lambda chat=None: [
        _R("2026-08-31T23:00:00+05:30")])
    import subprocess

    class _Fail:
        returncode = 1
        stderr = "sudo: a password is required"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Fail())
    assert wake.plan(NOW) is None
    assert wake._sudo_warned is True
    assert wake.plan(NOW) is None                  # no warn-spam


def test_one_broken_source_does_not_silence_the_rest(monkeypatch):
    from kyraan.triggers import agent_tasks, store

    def boom(chat=None):
        raise RuntimeError("pg down")
    monkeypatch.setattr(store, "list_pending", boom)
    monkeypatch.setattr(agent_tasks, "list_active", lambda chat=None: [
        _R("2026-08-31T23:00:00+05:30")])
    assert wake.next_due_time(NOW).isoformat() == "2026-08-31T23:00:00+05:30"
