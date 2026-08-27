"""P3.5e — review scaling per governance §6: the threshold math, the
24h objection window, and each full-review retrigger. Done-when: the
counters run live and the mode flips only when the policy says so."""
from datetime import datetime, timedelta, timezone

from kyraan.memory import review_scaling as scaling
from kyraan.memory import store as memory_store


def _earn_trust(total=200, approvals=50, rejections=0):
    for _ in range(total - approvals - rejections):
        scaling.record_decision(approved=True)   # older history
    for _ in range(rejections):
        scaling.record_decision(approved=False)
    for _ in range(approvals):
        scaling.record_decision(approved=True)


# --- threshold math -------------------------------------------------------

def test_mode_stays_full_below_200_total():
    _earn_trust(total=199)
    assert scaling.review_mode() == "full"


def test_mode_stays_full_below_90_percent():
    _earn_trust(total=250, approvals=44, rejections=6)  # trailing-50 = 88%
    assert scaling.review_mode() == "full"


def test_mode_flips_to_sampled_at_the_policy_line():
    _earn_trust(total=200, approvals=45, rejections=5)  # exactly 90%
    assert scaling.review_mode() == "sampled"


def test_every_third_proposal_holds_in_sampled_mode():
    _earn_trust()
    holds = [scaling.next_proposal_holds() for _ in range(6)]
    assert holds == [False, False, True, False, False, True]


def test_full_mode_holds_everything():
    assert all(scaling.next_proposal_holds() for _ in range(5))


# --- the 24h objection window ---------------------------------------------

def test_stamped_proposal_promotes_after_the_window(monkeypatch):
    _earn_trust()
    monkeypatch.setattr(memory_store, "promote",
                        lambda path, human=True: promoted.append((path, human)))
    promoted = []
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=23)).isoformat()
    for name, deadline in (("a", past), ("b", future)):
        (memory_store.PENDING_DIR / f"2026__{name}.md").write_text(
            f"---\ntarget: preferences/x.md\nreviewer: owner\n"
            f"auto_approve_after: {deadline}\n---\n\n- fact {name}\n")
    assert scaling.sweep_auto_approvals() == 1
    assert [h for _, h in promoted] == [False]   # never counts as human
    assert promoted[0][0].name == "2026__a.md"   # only the expired window


def test_unstamped_proposals_never_auto_promote(monkeypatch):
    promoted = []
    monkeypatch.setattr(memory_store, "promote",
                        lambda path, human=True: promoted.append(path))
    (memory_store.PENDING_DIR / "2026__held.md").write_text(
        "---\ntarget: preferences/x.md\nreviewer: owner\n---\n\n- held fact\n")
    assert scaling.sweep_auto_approvals() == 0
    assert promoted == []


def test_reject_during_window_is_the_objection():
    _earn_trust()
    path = memory_store.PENDING_DIR / "2026__obj.md"
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    path.write_text(f"---\ntarget: preferences/x.md\nreviewer: owner\n"
                    f"auto_approve_after: {past}\n---\n\n- objected fact\n")
    memory_store.reject(path)                    # the human objects in time
    assert scaling.sweep_auto_approvals() == 0   # nothing left to promote


# --- the retriggers -------------------------------------------------------

def test_wrong_auto_approval_retriggers():
    _earn_trust()
    assert scaling.review_mode() == "sampled"
    scaling.record_auto_approved("Auto approved fact about tea")
    scaling.on_forgotten(["Auto approved fact about tea"])
    assert scaling.review_mode() == "full"       # trailing window reset


def test_tier_change_retriggers(monkeypatch):
    _earn_trust()
    assert scaling.review_mode() == "sampled"
    monkeypatch.setattr(scaling, "_fingerprints",
                        lambda: {"tiers": "changed", "prompt": "same"})
    assert scaling.review_mode() == "full"


def test_prompt_change_retriggers(monkeypatch):
    _earn_trust()
    assert scaling.review_mode() == "sampled"
    fp = scaling._fingerprints()
    monkeypatch.setattr(scaling, "_fingerprints",
                        lambda: {**fp, "prompt": "deadbeef"})
    assert scaling.review_mode() == "full"


def test_trust_must_be_re_earned_after_retrigger():
    _earn_trust()
    scaling.retrigger("test")
    assert scaling.review_mode() == "full"
    for _ in range(50):                          # 50 fresh clean reviews
        scaling.record_decision(approved=True)
    assert scaling.review_mode() == "sampled"


# --- live wiring ----------------------------------------------------------

def test_promote_and_reject_drive_the_counters(tmp_path):
    p1 = memory_store.propose_fact("preferences/c1.md", "Counter fact one", "said")
    p2 = memory_store.propose_fact("preferences/c2.md", "Counter fact two", "said")
    memory_store.promote(p1)
    memory_store.reject(p2)
    stats = scaling._load()
    assert stats["total_reviewed"] == 2
    assert stats["recent"] == [1, 0]