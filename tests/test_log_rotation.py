"""events.jsonl is the audit trail — rotation must archive, never delete."""
from kyraan.control_plane import logging_setup


def test_log_rotates_at_size_threshold_archiving_old_events(monkeypatch, tmp_path):
    event_log = tmp_path / "events.jsonl"
    monkeypatch.setattr(logging_setup, "EVENT_LOG", event_log)
    monkeypatch.setattr(logging_setup, "_ROTATE_BYTES", 100)

    logging_setup.log_event("small")  # below threshold: no rotation
    assert len(list(tmp_path.glob("events-*.jsonl"))) == 0

    event_log.write_text("x" * 200 + "\n")  # push over the threshold
    logging_setup.log_event("first_after_rotation", detail="kept")

    archives = list(tmp_path.glob("events-*.jsonl"))
    assert len(archives) == 1
    assert "x" * 200 in archives[0].read_text()  # old events archived, not lost
    assert "first_after_rotation" in event_log.read_text()
    assert "x" * 200 not in event_log.read_text()  # fresh file started
