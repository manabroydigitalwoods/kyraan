

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
