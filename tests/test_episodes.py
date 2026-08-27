"""P3.3b — episode writer: chunker, twin-field selection (a record with
cloud_text must never embed raw text), idempotent ingest (kyraan_test
database; embedding and tagging faked — the live pair is proven by the
backfill run and test_embed)."""
import os
from pathlib import Path

import pytest

from kyraan.store import embed, episodes

_env = Path(__file__).resolve().parents[1] / ".env"
if "KYRAAN_PG_DSN" not in os.environ and _env.exists():
    for line in _env.read_text().splitlines():
        if line.startswith("KYRAAN_PG_DSN="):
            os.environ["KYRAAN_PG_DSN"] = line.split("=", 1)[1].strip()
            break

from kyraan.store import pg  # noqa: E402

_PG_UP = pg.available()


def _rec(role, text, ts="2026-08-27T10:00:00+00:00", chat_id=5, **extra):
    return {"ts": ts, "chat_id": chat_id, "role": role, "text": text, **extra}


# --- chunker --------------------------------------------------------------

def test_chunks_split_every_ten_user_turns():
    records = []
    for i in range(23):
        records.append(_rec("user", f"question {i}", f"2026-08-27T10:{i:02d}:00+00:00"))
        records.append(_rec("assistant", f"answer {i}", f"2026-08-27T10:{i:02d}:30+00:00"))
    chunks = episodes.chunk_day(records)[5]
    assert [sum(1 for _, l in c if l.startswith("user:")) for c in chunks] == [10, 10, 3]
    # forward-built: an existing chunk's FIRST ts is stable as the day grows
    assert chunks[0][0][0] == "2026-08-27T10:00:00+00:00"
    assert chunks[1][0][0] == "2026-08-27T10:10:00+00:00"


def test_chunks_are_per_chat():
    records = [_rec("user", "a", chat_id=1), _rec("user", "b", chat_id=2)]
    chunks = episodes.chunk_day(records)
    assert set(chunks) == {1, 2}


def test_episode_id_is_deterministic():
    assert episodes.episode_uuid(5, "t1") == episodes.episode_uuid(5, "t1")
    assert episodes.episode_uuid(5, "t1") != episodes.episode_uuid(6, "t1")


# --- the privacy twin rule ------------------------------------------------

def test_cloud_text_twin_wins_over_raw_text():
    entry = _rec("assistant", "RAW BODY: the private email content",
                 cloud_text="[showed the unread email summary]")
    assert episodes.cloud_line(entry) == "assistant: [showed the unread email summary]"


def test_legacy_assistant_record_gets_placeholder():
    entry = _rec("assistant", "You have about 4 unread emails. Latest unread:\n- X: secret")
    assert episodes.cloud_line(entry) == "assistant: [showed the unread email summary]"


def test_user_and_proactive_records_pass_through():
    assert episodes.cloud_line(_rec("user", "hello")) == "user: hello"
    assert episodes.cloud_line(_rec("proactive", "⏰ water")) == "assistant: ⏰ water"


def test_non_conversation_records_skip():
    assert episodes.cloud_line(_rec("system", "boot")) is None
    assert episodes.cloud_line(_rec("user", "   ")) is None


# --- idempotent ingest (pg) -----------------------------------------------

@pytest.fixture
def episode_db(monkeypatch):
    if not _PG_UP:
        pytest.skip("local Postgres container unreachable")
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    with pg.connection() as conn:
        conn.execute("TRUNCATE episode")
        conn.commit()
    yield
    pg.reset_pool_for_tests()


def _fake_embed(texts):
    return [[0.5] * embed.EMBED_DIM for _ in texts]


@pytest.mark.pg
def test_ingest_is_idempotent_and_grows_in_place(episode_db, monkeypatch):
    monkeypatch.setattr(embed, "embed", _fake_embed)
    day = "2026-08-27"
    records = [_rec("user", "how's traffic"),
               _rec("assistant", "42 min", "2026-08-27T10:00:30+00:00")]
    assert episodes.ingest_day(day, records, tag=lambda t: [])["episodes"] == 1
    assert episodes.ingest_day(day, records, tag=lambda t: [])["episodes"] == 1
    with pg.connection() as conn:
        count, = conn.execute("SELECT count(*) FROM episode").fetchone()
    assert count == 1  # re-run upserted, not duplicated
    # the trailing chunk grows in place when later messages arrive
    records.append(_rec("user", "and to Jalpaiguri?", "2026-08-27T10:01:00+00:00"))
    records.append(_rec("assistant", "55 min", "2026-08-27T10:01:30+00:00"))
    episodes.ingest_day(day, records, tag=lambda t: [])
    with pg.connection() as conn:
        count, text = conn.execute(
            "SELECT count(*), max(text) FROM episode").fetchone()
    assert count == 1 and "Jalpaiguri" in text


@pytest.mark.pg
def test_flags_and_metadata_land(episode_db, monkeypatch):
    monkeypatch.setattr(embed, "embed", _fake_embed)
    records = [_rec("user", "my chest hurts a bit")]
    episodes.ingest_day("2026-08-27", records, tag=lambda t: ["health"])
    with pg.connection() as conn:
        row = conn.execute(
            """SELECT flags, participants, visibility, exposure, day::text
               FROM episode""").fetchone()
    assert row == (["health"], ["owner"], "owner", "cloud_ok", "2026-08-27")


def test_tagging_failure_defaults_to_sensitive(monkeypatch):
    from kyraan.model_router import router

    def boom(**kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(router, "call", boom)
    assert episodes.sensitivity_flags("anything") == ["sensitive"]
