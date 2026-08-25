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
        scheduler.create_reminder(chat_id=0, text="test", when_iso="2026-08-25T13:26:44+05:30+04:00")

    assert store.list_pending() == []


def test_init_skips_a_corrupted_persisted_reminder_instead_of_crashing(isolated_store):
    """Defense in depth for any record that predates the validate-before-
    persist fix (or reaches disk some other way) — one bad reminder must
    not take down scheduling for every other reminder on startup."""
    store.add(chat_id=0, text="bad", when_iso="2026-08-25T13:26:44+05:30+04:00")
    store.add(chat_id=0, text="good", when_iso="2026-08-25T17:30:00+05:30")

    scheduled_ids = []
    scheduler.init(
        schedule_fn=lambda job_name, run_at, payload: scheduled_ids.append(job_name),
        cancel_fn=lambda *a, **k: None,
        send_fn=None,
    )

    assert len(scheduled_ids) == 1  # only the good one was scheduled, no exception raised
