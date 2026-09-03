"""Kiaan's keeper — duty #1 (2026-09-03)."""
import asyncio
from datetime import date

import pytest

from kyraan.triggers import kiaan_keeper as k

CARD = """1. B.C.G. & O.P.V.
From the 1st Day of Birth
Date of Immunisation:

2. HEPATITIS-B (O-DATE ONWARD)
DOSE | Administered on | Next Appointment
First | 13/10/25 |
Second |  |

5. Combination Vaccine After 45 Days
DOSE | Administered on | Next Appointment
1. First | 30 NOV 2025 |
2. Second | 04 JAN 2026 |
3. Third |  |

6. ROTAVIRUS (1st Dose)
1st Dose | 01 NOV 2025 | Next Dose After 1-2 month

8. Pneumococcal-13
1 | 4 JAN 2026 |
"""


def test_card_dates_map_to_doses_in_order():
    done = k.done_from_card(CARD)
    assert done["hepb1"] == "2025-10-13"
    assert done["penta1"] == "2025-11-30" and done["penta2"] == "2026-01-04"   # dose lines don't split the block
    assert done["rota1"] == "2025-11-01" and done["pcv1"] == "2026-01-04"
    assert "bcg" not in done                                                   # no date, not done


def test_upcoming_by_age_with_statuses():
    born = date(2025, 10, 12)
    today = date(2026, 9, 3)                      # 10.7 months
    done = {"mr1": {}, "je1": {}}
    up = {sid: st for sid, _l, _d, st in k.upcoming(born, done, today)}
    assert up["tcv"] == "overdue" and up["hepa1"] == "later"
    assert "mr1" not in up
    assert k.milestone_windows(born, today) and any(m == "wave" for m, _ in k.milestone_windows(born, today))
    # three weeks before the first birthday, the 12-month doses are due-soon
    up = {sid: st for sid, _l, _d, st in k.upcoming(born, done, date(2026, 9, 25))}
    assert up["hepa1"] == "due-soon"


def test_mark_done_and_skip(monkeypatch, tmp_path):
    monkeypatch.setattr(k, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(k, "card_text", lambda: "")
    monkeypatch.setattr(k, "visit_dates", lambda: [])
    monkeypatch.setattr(k, "birth_date", lambda: date(2025, 10, 12))
    assert k.mark_done("MR", when=date(2026, 9, 2)) == ["Measles-Rubella (MR) — 1st"]
    assert k.mark_done("typhoid", skipped=True) == ["Typhoid conjugate (TCV)"]
    dm = k.done_map()
    assert dm["mr1"]["source"] == "you said" and dm["tcv"]["source"].startswith("skipped")
    assert k.mark_done("MR") == []                                            # already recorded


def test_morning_check_speaks_once_and_respects_delivery(monkeypatch, tmp_path):
    monkeypatch.setattr(k, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(k, "card_text", lambda: "")
    monkeypatch.setattr(k, "visit_dates", lambda: [])
    monkeypatch.setattr(k, "birth_date", lambda: date(2025, 10, 12))
    monkeypatch.setattr(k, "milestone_recorded", lambda label: False)
    monkeypatch.setattr(k.kernel, "can_send_proactively", lambda **kw: True)

    class _Now:
        @staticmethod
        def date(): return date(2026, 9, 25)
    monkeypatch.setattr(k, "local_now", lambda: _Now())
    sent = []

    async def send(chat_id, text):
        sent.append(text); return True
    assert asyncio.run(k.fire(1, send)) is True
    assert "Hepatitis A" in sent[0] and "due around 12 Oct" in sent[0]
    assert asyncio.run(k.fire(1, send)) is False                            # nothing new tomorrow
    assert len(sent) == 1

    async def fail(chat_id, text):
        sent.append(text); return False
    monkeypatch.setattr(k, "STATE_PATH", tmp_path / "s2.json")
    assert asyncio.run(k.fire(1, fail)) is False
    assert asyncio.run(k.fire(1, send)) is True                             # undelivered = say it again


def test_status_and_record_rails(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    monkeypatch.setattr(k, "status_text", lambda: "Kiaan is 10 months old.")
    assert asyncio.run(orchestrator.handle_message(1, "kiaan status")).startswith("Kiaan is 10")
    assert asyncio.run(orchestrator.handle_message(1, "when is kiaan's next vaccine?")).startswith("Kiaan is 10")
    asked = []

    async def fake_gated(chat_id, call, handler, describe="", **kw):
        asked.append((call.skill_name, call.args, describe)); return "ASK"
    monkeypatch.setattr(orchestrator, "_gated", fake_gated)
    assert asyncio.run(orchestrator.handle_message(1, "kiaan got the MR shot today")) == "ASK"
    assert asked[0][1] == {"words": "MR", "skipped": False} and "got" in asked[0][2]
    assert asyncio.run(orchestrator.handle_message(1, "kiaan skipped typhoid")) == "ASK"
    assert asked[1][1]["skipped"] is True
