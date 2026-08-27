"""events.jsonl is the audit trail — rotation must archive, never delete."""
from kyraan.control_plane import logging_setup


def test_log_rotates_at_size_threshold_archiving_old_events(monkeypatch, tmp_path):
    event_log = tmp_path / "events.jsonl"
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr(logging_setup, "EVENT_LOG", event_log)
    monkeypatch.setattr(logging_setup, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(logging_setup, "_ROTATE_BYTES", 100)

    logging_setup.log_event("small")  # below threshold: no rotation
    assert len(list(archive_dir.rglob("events-*.jsonl"))) == 0

    event_log.write_text("x" * 200 + "\n")  # push over the threshold
    logging_setup.log_event("first_after_rotation", detail="kept")

    archives = list(archive_dir.rglob("events-*.jsonl"))  # in today's day folder
    assert len(archives) == 1
    assert "x" * 200 in archives[0].read_text()  # old events archived, not lost
    assert "first_after_rotation" in event_log.read_text()
    assert "x" * 200 not in event_log.read_text()  # fresh file started


def test_tests_never_write_the_production_audit_log():
    """The autouse conftest fixture must have redirected EVENT_LOG away
    from the real logs/ directory for every test in the suite."""
    assert "logs" not in str(logging_setup.EVENT_LOG.parent.name) or "pytest" in str(logging_setup.EVENT_LOG)
    logging_setup.log_event("test_isolation_probe")
    real = logging_setup.LOG_DIR / "events.jsonl"
    if real.exists():
        assert "test_isolation_probe" not in real.read_text()[-2000:]


def test_new_day_rotates_into_the_days_folder(monkeypatch, tmp_path):
    """Live files hold only TODAY: the first write of a new local day
    archives yesterday's file under archive/YYYY-MM-DD/, size regardless.
    chat.jsonl is exempt — restart seeding reads it across midnight."""
    import os
    import time

    event_log = tmp_path / "events.jsonl"
    chat_log = tmp_path / "chat.jsonl"
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr(logging_setup, "EVENT_LOG", event_log)
    monkeypatch.setattr(logging_setup, "CHAT_LOG", chat_log)
    monkeypatch.setattr(logging_setup, "ARCHIVE_DIR", archive_dir)

    logging_setup.log_event("yesterday_evening")
    logging_setup.log_chat(1, "user", "good night")
    two_days_ago = time.time() - 2 * 86400
    os.utime(event_log, (two_days_ago, two_days_ago))
    os.utime(chat_log, (two_days_ago, two_days_ago))

    logging_setup.log_event("good_morning")
    logging_setup.log_chat(1, "user", "good morning")

    day_dirs = [p for p in archive_dir.iterdir() if p.is_dir()]
    assert len(day_dirs) == 1                        # yesterday's folder
    archived = list(day_dirs[0].glob("events-*.jsonl"))
    assert len(archived) == 1
    assert "yesterday_evening" in archived[0].read_text()
    assert "good_morning" in event_log.read_text()   # fresh live file
    assert "good night" in chat_log.read_text()       # chat NOT day-rotated
