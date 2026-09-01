"""The correction→behavior loop (2026-09-01): deterministic clustering,
local-tier drafting to pending review, owner-gated adoption, prompt
rendering, retirement."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from kyraan.memory import lessons


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(lessons, "RULES_PATH", tmp_path / "learned_rules.json")
    from kyraan.control_plane import logging_setup
    monkeypatch.setattr(logging_setup, "EVENT_LOG", tmp_path / "events.jsonl")
    monkeypatch.setattr(logging_setup, "ARCHIVE_DIR", tmp_path / "none")
    from kyraan.memory import store
    monkeypatch.setattr(store, "PENDING_DIR", tmp_path / "pending")
    yield tmp_path


def _log_corrections(tmp_path, entries):
    now = datetime.now(timezone.utc)
    with open(tmp_path / "events.jsonl", "w") as f:
        for days_ago, text in entries:
            ts = (now - timedelta(days=days_ago)).isoformat()
            f.write(json.dumps({"ts": ts,
                                "kind": "user_correction_candidate",
                                "correction": text}) + "\n")


def test_clusters_need_three_hits_over_two_days(isolated):
    _log_corrections(isolated, [
        (2, "use bullet points please make it readable"),
        (1, "again use bullet points, this is not readable"),
        (0, "bullet points! make replies readable"),
        (0, "no that meeting was tuesday"),          # singleton: no cluster
    ])
    ready = lessons.find_clusters()
    assert len(ready) == 1
    assert len(ready[0][1]) == 3


def test_same_day_repeats_do_not_qualify(isolated):
    _log_corrections(isolated, [
        (0, "use bullet points please"),
        (0, "use bullet points again"),
        (0, "bullet points please use them")])
    assert lessons.find_clusters() == []


async def test_scan_drafts_locally_and_queues_for_review(isolated, monkeypatch):
    _log_corrections(isolated, [
        (2, "use bullet points please make it readable"),
        (1, "again use bullet points, not readable"),
        (0, "bullet points! keep replies readable")])
    tiers = []

    class _R:
        text = "Format every list reply as short bullet points."

    async def fake_acall(prompt="", system="", tier="", **kw):
        tiers.append(tier)
        assert "bullet points" in prompt
        return _R()

    from kyraan.model_router import router
    monkeypatch.setattr(router, "acall", fake_acall)
    assert await lessons.scan_and_propose() == 1
    assert tiers == ["cheap"]                      # local tier ONLY
    from kyraan.memory import store
    pending = list(store.PENDING_DIR.glob("*persona*"))
    assert len(pending) == 1
    body = pending[0].read_text()
    assert "target: persona/" in body
    assert "Format every list reply" in body
    # the fingerprint is spent: a rescan proposes nothing
    assert await lessons.scan_and_propose() == 0


def test_apply_render_and_retire_round_trip(isolated):
    rid = lessons.apply("Keep list lines under ten words.", ["src a"])
    assert "under ten words" in lessons.block()
    assert "approved by the owner" in lessons.block()
    gone = lessons.retire("ten words")
    assert gone["id"] == rid
    assert lessons.block() == ""
    with pytest.raises(ValueError, match="no active learned rule"):
        lessons.retire("ten words")


def test_active_cap_holds(isolated):
    for i in range(12):
        lessons.apply(f"Rule number {i} about thing {i}.", [])
    assert len(lessons.active_rules()) == lessons.MAX_ACTIVE
