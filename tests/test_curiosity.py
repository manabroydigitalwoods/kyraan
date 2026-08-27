"""Curiosity queue (Phase 4 closeout, 2026-08-28): deterministic
knowledge-gap questions, one a day, 14-day re-ask memory."""
import pytest

from kyraan.triggers import curiosity


@pytest.fixture
def gaps(monkeypatch):
    from kyraan.agents import faces
    from kyraan.memory import engine
    from kyraan.memory import store as memory_store
    from kyraan.store import persons
    monkeypatch.setattr(faces, "available", lambda: True)
    monkeypatch.setattr(faces, "enrolled_names",
                        lambda: ["Akansha (employee)", "Kamal"])
    monkeypatch.setattr(persons, "list_persons",
                        lambda: [("owner",), ("kamal",), ("suman_ghosh",)])
    monkeypatch.setattr(persons, "name_map",
                        lambda: {"owner": "owner", "kamal": "kamal",
                                 "suman": "suman_ghosh",
                                 "suman ghosh": "suman_ghosh"})
    monkeypatch.setattr(engine, "active_entries",
                        lambda: [{"content": "Kamal is a friend",
                                  "flags": []}])
    return monkeypatch


def test_candidates_come_from_real_gaps(gaps):
    got = dict(curiosity.collect_candidates(7))
    # face enrolled but unregistered ("akansha_employee" slug)
    assert any(k.startswith("face_unregistered:akansha") for k in got)
    # registered person with zero facts naming them
    assert "person_blank:suman_ghosh" in got
    # kamal HAS a fact -> no blank question for him
    assert "person_blank:kamal" not in got


def test_one_question_a_day_and_no_repeats(gaps):
    first = curiosity.daily_line(7)
    assert first and first.startswith("🤔")
    second = curiosity.daily_line(7)
    assert second != first          # the next gap, never the same one
    # exhaust the queue -> quiet days are normal
    while curiosity.daily_line(7):
        pass
    assert curiosity.daily_line(7) is None
