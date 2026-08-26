

def test_duplicate_detection_survives_rephrasing():
    """Eval failure 2026-08-26: first create said 'Call mom', the repeat
    request sent 'Remind me to call mom' — exact-text matching created a
    second ping for one intent. Identity = same moment + overlapping
    content words, filler stripped."""
    from kyraan.triggers import scheduler

    when = "2026-08-27T21:00:00+05:30"
    scheduler.create_reminder(91, "Call mom", when)
    assert scheduler.find_duplicate(91, "Remind me to call mom", when) is not None
    assert scheduler.find_duplicate(91, "call Mom please", when) is not None
    # different intent at the same time is NOT a duplicate
    assert scheduler.find_duplicate(91, "take medicine", when) is None
    # same intent at a different time is NOT a duplicate
    assert scheduler.find_duplicate(91, "Call mom", "2026-08-27T20:00:00+05:30") is None


def test_recurring_series_is_duplicate_regardless_of_next_occurrence():
    """Live 2026-08-26: re-requesting the hourly water series passed the
    moment check via a different when_iso (today 7 PM vs tomorrow 10 AM)
    and created a second identical series — double pings every hour. A
    recurring reminder is a SERIES: same intent + both recurring = one."""
    from kyraan.triggers import scheduler

    scheduler.create_reminder(
        92, "Drink water", "2026-08-26T19:00:00+05:30", repeat="interval",
        interval_minutes=60, window_start="10:00", window_end="21:00")
    dup = scheduler.find_duplicate(
        92, "drink water", "2026-08-27T10:00:00+05:30", repeat="interval")
    assert dup is not None
    # even a different recurrence kind of the same intent is the series
    assert scheduler.find_duplicate(
        92, "Drink water", "2026-08-27T10:00:00+05:30", repeat="daily") is not None
    # a ONE-SHOT of the same text at a different moment is NOT the series
    assert scheduler.find_duplicate(
        92, "Drink water", "2026-08-27T17:00:00+05:30") is None
    # different intent recurring is not a duplicate
    assert scheduler.find_duplicate(
        92, "take medicine", "2026-08-27T10:00:00+05:30", repeat="daily") is None
