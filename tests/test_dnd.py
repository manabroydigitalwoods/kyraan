import os
from datetime import datetime, timezone

os.environ.setdefault("KYRAAN_TIMEZONE", "UTC")

from kyraan.control_plane import dnd


def test_quiet_hours_wraps_midnight():
    late_night = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
    early_morning = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    midday = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)

    assert dnd.in_quiet_hours(late_night)
    assert dnd.in_quiet_hours(early_morning)
    assert not dnd.in_quiet_hours(midday)
