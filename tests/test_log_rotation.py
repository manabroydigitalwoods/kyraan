"""events.jsonl is the audit trail — rotation must archive, never delete."""
from kyraan.control_plane import logging_setup


def test_log_rotates_at_size_threshold_archiving_old_events(monkeypatch, tmp_path):
    event_log = tmp_path / "events.jsonl"
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr(logging_setup, "EVENT_LOG", event_log)
    monkeypatch.setattr(logging_setup, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(logging_setup, "_ROTATE_BYTES", 100)

    logging_setup.log_event("small")  # below threshold: no rotation
    assert len(list(archive_dir.glob("events-*.jsonl"))) == 0

    event_log.write_text("x" * 200 + "\n")  # push over the threshold
    logging_setup.log_event("first_after_rotation", detail="kept")

    archives = list(archive_dir.glob("events-*.jsonl"))
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
