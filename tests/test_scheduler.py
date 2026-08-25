import os
from datetime import timezone

import pytest

os.environ.setdefault("KYRAAN_TIMEZONE", "UTC")

from kyraan.triggers import scheduler, store
from kyraan.triggers.scheduler import _parse_when


def test_parse_when_keeps_offset_if_present():
    parsed = _parse_when("2026-08-25T17:30:00+05:30")
    assert parsed.utcoffset().total_seconds() == 5.5 * 3600


def test_parse_when_attaches_local_tz_if_naive():
    """Regression test: a naive when_iso (a model dropping the UTC offset it
    was asked for) used to crash scheduling code that compared it against
    an aware datetime. It must come back aware instead."""
    parsed = _parse_when("2026-08-25T17:30:00")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    """store.py uses a fixed real path (data/reminders.json) with no
    built-in test seam — redirect it to a tmp file so these tests can't
    touch production reminder data."""
    monkeypatch.setattr(store, "REMINDERS_PATH", tmp_path / "reminders.json")
    yield


def test_create_reminder_rejects_bad_datetime_without_persisting(isolated_store):
    """Regression test for a live crash: a model producing a duplicated UTC
    offset ("+05:30+04:00") got persisted to disk *before* the unparseable
    datetime raised — turning every future scheduler.init() (i.e. every
    app startup) into an immediate crash. Validation must happen before
    the write, not after."""
    scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=None)

    with pytest.raises(ValueError):
        scheduler.create_reminder(chat_id=0, text="test", when_iso="2026-13-45Tnot-a-time")

    assert store.list_pending() == []


def test_init_skips_a_corrupted_persisted_reminder_instead_of_crashing(isolated_store):
    """Defense in depth for any record that predates the validate-before-
    persist fix (or reaches disk some other way) — one bad reminder must
    not take down scheduling for every other reminder on startup."""
    store.add(chat_id=0, text="bad", when_iso="2026-13-45Tnot-a-time")
    store.add(chat_id=0, text="good", when_iso="2026-08-25T17:30:00+05:30")

    scheduled_ids = []
    scheduler.init(
        schedule_fn=lambda job_name, run_at, payload: scheduled_ids.append(job_name),
        cancel_fn=lambda *a, **k: None,
        send_fn=None,
    )

    assert len(scheduled_ids) == 1  # only the good one was scheduled, no exception raised


def test_overdue_reminder_fires_now_with_a_late_note(isolated_store):
    """A reminder whose due time passed while the app was down used to be
    handed to the channel scheduler with a past timestamp — whatever
    happens then (JobQueue fires immediately, silently pretending to be on
    time) was undefined behavior, not a choice. Now it's explicit: fire
    once, now, annotated as late."""
    from kyraan.control_plane.dnd import local_now

    store.add(chat_id=0, text="water plants", when_iso="2020-01-01T10:00:00+00:00")

    captured = {}
    scheduler.init(
        schedule_fn=lambda name, run_at, payload: captured.update(run_at=run_at, payload=payload),
        cancel_fn=lambda *a, **k: None,
        send_fn=None,
    )

    assert "(was due 2020-01-01T10:00:00+00:00)" in captured["payload"]["text"]
    assert abs((captured["run_at"] - local_now()).total_seconds()) < 5


def test_future_reminder_is_scheduled_unannotated(isolated_store):
    store.add(chat_id=0, text="water plants", when_iso="2099-01-01T10:00:00+00:00")

    captured = {}
    scheduler.init(
        schedule_fn=lambda name, run_at, payload: captured.update(run_at=run_at, payload=payload),
        cancel_fn=lambda *a, **k: None,
        send_fn=None,
    )

    assert captured["payload"]["text"] == "water plants"
    assert captured["run_at"] == _parse_when("2099-01-01T10:00:00+00:00")


async def test_fire_is_idempotent_against_duplicate_scheduling(isolated_store):
    """Found live: a reminder fired twice 0.8s apart when two bot processes
    briefly overlapped across a restart. fire() must consult the store and
    send at most once, no matter how many jobs point at the reminder."""
    r = store.add(chat_id=0, text="water plants", when_iso="2099-01-01T10:00:00+00:00")
    sends = []

    async def send_fn(chat_id, text):
        sends.append(text)

    scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=send_fn)

    await scheduler.fire(r.id, 0, r.text)  # first fire: sends
    await scheduler.fire(r.id, 0, r.text)  # duplicate job: must not send
    assert sends == ["Reminder: water plants"]

    store.cancel(r.id)
    r2 = store.add(chat_id=0, text="other", when_iso="2099-01-01T10:00:00+00:00")
    store.cancel(r2.id)
    await scheduler.fire(r2.id, 0, r2.text)  # cancelled record: must not send
    assert sends == ["Reminder: water plants"]


def test_utc_offset_from_model_is_reinterpreted_as_local_wall_time(monkeypatch):
    """Found live: "call suman at 7pm" extracted as 19:00:00.000Z — the
    model dropped the +05:30 offset to Z, which would have fired at 00:30
    local, 5.5 hours late. A Z timestamp in a non-UTC KYRAAN_TIMEZONE is
    offset-dropping, not UTC intent: keep the wall time, fix the zone."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    parsed = _parse_when("2026-08-25T19:00:00.000Z")
    assert parsed.hour == 19
    assert parsed.utcoffset().total_seconds() == 5.5 * 3600


def test_genuine_utc_in_a_utc_timezone_is_untouched(monkeypatch):
    monkeypatch.setenv("KYRAAN_TIMEZONE", "UTC")
    parsed = _parse_when("2026-08-25T19:00:00+00:00")
    assert parsed.hour == 19
    assert parsed.utcoffset().total_seconds() == 0


def test_non_utc_offset_from_model_is_respected(monkeypatch):
    """Only a zero offset triggers reinterpretation — an explicit non-UTC
    offset from the model is kept exactly."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    parsed = _parse_when("2026-08-25T19:00:00+02:00")
    assert parsed.utcoffset().total_seconds() == 2 * 3600


async def test_withheld_delivery_is_logged_truthfully_not_as_sent(isolated_store):
    """Found in a live audit: the owner-only guard retired a dev-harness
    reminder but events.jsonl said reminder_sent. A send_fn returning
    False now logs reminder_retired_undelivered; the record still retires
    either way so it never fires again."""
    from kyraan.control_plane import logging_setup

    r = store.add(chat_id=0, text="dev record", when_iso="2099-01-01T10:00:00+00:00")

    async def withholding_send_fn(chat_id, text):
        return False  # owner-only guard path

    scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=withholding_send_fn)
    await scheduler.fire(r.id, 0, r.text)

    log = logging_setup.EVENT_LOG.read_text()
    assert "reminder_retired_undelivered" in log
    assert '"reminder_sent"' not in log
    assert store.get(r.id).sent is True  # retired — will never fire again


async def test_legacy_send_fn_returning_none_still_logs_sent(isolated_store):
    from kyraan.control_plane import logging_setup

    r = store.add(chat_id=0, text="real send", when_iso="2099-01-01T10:00:00+00:00")

    async def legacy_send_fn(chat_id, text):
        pass  # returns None, like the dev-harness send_fns

    scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None, send_fn=legacy_send_fn)
    await scheduler.fire(r.id, 0, r.text)
    assert '"reminder_sent"' in logging_setup.EVENT_LOG.read_text()


def test_init_only_chat_never_schedules_other_chats_reminders(isolated_store):
    """Audit finding: a dev harness scheduled the OWNER's pending reminders
    with its console send_fn — firing first, marking sent, and suppressing
    the real Telegram delivery. only_chat fences harnesses to their own."""
    store.add(chat_id=0, text="dev thing", when_iso="2099-01-01T10:00:00+00:00")
    store.add(chat_id=42, text="owner's real reminder", when_iso="2099-01-01T11:00:00+00:00")

    scheduled = []
    scheduler.init(
        schedule_fn=lambda name, run_at, payload: scheduled.append(payload["text"]),
        cancel_fn=lambda *a, **k: None, send_fn=None, only_chat=0,
    )
    assert scheduled == ["dev thing"]


def test_z_glued_to_offset_is_repaired(monkeypatch):
    """Seen live: '2026-08-26T09:00:00.000Z+05:30' — the composed
    date/time were right, only the format was junk."""
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    parsed = _parse_when("2026-08-26T09:00:00.000Z+05:30")
    assert (parsed.hour, parsed.minute) == (9, 0)
    assert parsed.utcoffset().total_seconds() == 5.5 * 3600


def test_doubled_offset_keeps_the_first(monkeypatch):
    monkeypatch.setenv("KYRAAN_TIMEZONE", "Asia/Kolkata")
    parsed = _parse_when("2026-08-25T13:26:44+05:30+04:00")
    assert parsed.utcoffset().total_seconds() == 5.5 * 3600
