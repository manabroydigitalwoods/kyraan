import os
from datetime import timezone

os.environ.setdefault("KYRAAN_TIMEZONE", "UTC")

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
