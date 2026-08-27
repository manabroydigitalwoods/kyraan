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


# --- recall (P3.3c) -------------------------------------------------------

def _seed_episode(chat_id, day, text, flags=(), vector=None):
    import json as _json
    with pg.connection() as conn:
        conn.execute(
            """INSERT INTO episode (id, chat_id, day, flags, text, embedding,
                                    created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (episodes.episode_uuid(chat_id, f"{day}:{text}"), chat_id, day,
             list(flags), text,
             _json.dumps(vector or [0.5] * embed.EMBED_DIM),
             f"{day}T10:00:00+00:00"))
        conn.commit()


@pytest.mark.pg
def test_recall_labels_scopes_and_caps(episode_db, monkeypatch):
    monkeypatch.setattr(embed, "embed", _fake_embed)
    for i in range(12):
        _seed_episode(5, "2026-08-20", f"user: traffic to Jalpaiguri run {i}")
    _seed_episode(6, "2026-08-20", "user: another chat's episode")
    lines = episodes.recall(5, "traffic to Jalpaiguri", k=20)
    assert len(lines) == episodes.RECALL_K_MAX  # cap, even when asked for 20
    assert all(line.startswith("[recalled from 2026-08-20] ") for line in lines)
    assert not any("another chat" in line for line in lines)  # chat-scoped


@pytest.mark.pg
def test_recall_discretion_needs_a_direct_hit(episode_db, monkeypatch):
    monkeypatch.setattr(embed, "embed", _fake_embed)
    # identical vectors: ANN alone would surface both; discretion must not
    _seed_episode(5, "2026-08-20", "user: my chest pain scare details",
                  flags=["sensitive"])
    _seed_episode(5, "2026-08-20", "user: weather in Kolkata")
    unrelated = episodes.recall(5, "weather in Kolkata")
    assert not any("chest pain" in line for line in unrelated)  # absence
    direct = episodes.recall(5, "chest pain scare")
    assert any("chest pain" in line for line in direct)  # direct hit surfaces


@pytest.mark.pg
def test_recall_dedups_near_identical_episodes(episode_db, monkeypatch):
    monkeypatch.setattr(embed, "embed", _fake_embed)
    same = "user: remind me to call mom at 9pm " + "x" * 120
    for day in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _seed_episode(5, day, same + day)  # same first 120 chars
    _seed_episode(5, "2026-08-23", "user: a different conversation entirely")
    lines = episodes.recall(5, "call mom reminder", k=5)
    assert len(lines) == 2  # one representative + the distinct one


@pytest.mark.pg
def test_recall_excludes_suppressed(episode_db, monkeypatch):
    monkeypatch.setattr(embed, "embed", _fake_embed)
    _seed_episode(5, "2026-08-20", "user: the forgotten topic")
    with pg.connection() as conn:
        conn.execute("UPDATE episode SET suppressed_by = %s",
                     (["11111111-1111-1111-1111-111111111111"],))
        conn.commit()
    assert episodes.recall(5, "forgotten topic") == []


# --- forget cascade (P3.3d) -----------------------------------------------

_FID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.mark.pg
def test_forgotten_fact_makes_matching_episode_unfindable(episode_db, monkeypatch):
    monkeypatch.setattr(embed, "embed", _fake_embed)
    _seed_episode(5, "2026-08-20", "user: my favourite fruit is dragonfruit")
    assert episodes.recall(5, "favourite fruit dragonfruit") != []
    swept = episodes.suppress_for_fact(_FID, "Favourite fruit is dragonfruit")
    assert swept == 1
    assert episodes.recall(5, "favourite fruit dragonfruit") == []


@pytest.mark.pg
def test_suppression_is_auditable_and_idempotent(episode_db, monkeypatch):
    monkeypatch.setattr(embed, "embed", _fake_embed)
    _seed_episode(5, "2026-08-20", "user: my favourite fruit is dragonfruit")
    episodes.suppress_for_fact(_FID, "Favourite fruit is dragonfruit")
    assert episodes.suppress_for_fact(_FID, "Favourite fruit is dragonfruit") == 0
    with pg.connection() as conn:
        suppressed, = conn.execute(
            "SELECT suppressed_by FROM episode").fetchone()
    assert [str(u) for u in suppressed] == [_FID]  # who forgot what: on the row


@pytest.mark.pg
def test_single_word_overlap_does_not_sweep(episode_db, monkeypatch):
    monkeypatch.setattr(embed, "embed", _fake_embed)
    _seed_episode(5, "2026-08-20", "user: what a lovely favourite spot")
    # only 'favourite' overlaps — below the fixed overlap>=2 threshold
    assert episodes.suppress_for_fact(_FID, "Favourite fruit is dragonfruit") == 0


@pytest.mark.pg
def test_fact_refs_hit_sweeps_regardless_of_words(episode_db, monkeypatch):
    import json as _json
    monkeypatch.setattr(embed, "embed", _fake_embed)
    with pg.connection() as conn:
        conn.execute(
            """INSERT INTO episode (id, chat_id, day, fact_refs, text,
                                    embedding, created_at)
               VALUES (%s, 5, '2026-08-20', %s::uuid[], 'user: unrelated words',
                       %s, '2026-08-20T10:00:00+00:00')""",
            (episodes.episode_uuid(5, "refs-test"), [_FID],
             _json.dumps([0.5] * embed.EMBED_DIM)))
        conn.commit()
    assert episodes.suppress_for_fact(_FID, "zzz qqq") == 1


@pytest.mark.pg
def test_delete_me_removes_rows_by_participant(episode_db, monkeypatch):
    import json as _json
    monkeypatch.setattr(embed, "embed", _fake_embed)
    with pg.connection() as conn:
        for name, who in (("a", ["owner"]), ("b", ["ruma", "owner"])):
            conn.execute(
                """INSERT INTO episode (id, chat_id, day, participants, text,
                                        embedding, created_at)
                   VALUES (%s, 5, '2026-08-20', %s, 'user: hi', %s,
                           '2026-08-20T10:00:00+00:00')""",
                (episodes.episode_uuid(5, f"del-{name}"), who,
                 _json.dumps([0.5] * embed.EMBED_DIM)))
        conn.commit()
    assert episodes.delete_person_episodes("ruma") == 1
    with pg.connection() as conn:
        remaining, = conn.execute("SELECT count(*) FROM episode").fetchone()
    assert remaining == 1  # the owner-only episode survives — gone means gone


def test_engine_forget_triggers_the_sweep(monkeypatch):
    from kyraan.memory import engine
    from kyraan.store import facts
    monkeypatch.setattr(facts, "MIRROR_ENABLED", True)
    monkeypatch.setattr(facts, "mirror_entries", lambda entries: True)
    swept = []
    monkeypatch.setattr(episodes, "suppress_for_fact",
                        lambda fid, content: swept.append((fid, content)) or 1)
    fid = engine.add_fact("Sweep hook probe fact", "preferences/sweep.md", "test")
    engine.forget([fid])
    assert swept == [(facts.fact_uuid(fid), "Sweep hook probe fact")]


def test_sweep_failure_never_breaks_forget(monkeypatch):
    from kyraan.memory import engine
    from kyraan.store import facts
    monkeypatch.setattr(facts, "MIRROR_ENABLED", True)
    monkeypatch.setattr(facts, "mirror_entries", lambda entries: True)

    def boom(fid, content):
        raise RuntimeError("pg down")

    monkeypatch.setattr(episodes, "suppress_for_fact", boom)
    events = []
    monkeypatch.setattr(engine, "log_event",
                        lambda name, **kw: events.append(name))
    fid = engine.add_fact("Deferred sweep fact", "preferences/sweep2.md", "test")
    assert engine.forget([fid]) == ["Deferred sweep fact"]
    assert "episode_suppress_deferred" in events


# --- the loop tool executor (faked store) ---------------------------------

def test_recall_executor_passes_through_lines(monkeypatch):
    import asyncio

    from kyraan.agents import loop_tools
    monkeypatch.setattr(episodes, "recall",
                        lambda chat_id, query, k: ["[recalled from 2026-08-20] user: hi"])
    result = asyncio.run(loop_tools._memory_recall(5, {"query": "old topic"}, ""))
    assert result == ["[recalled from 2026-08-20] user: hi"]


def test_recall_executor_empty_is_honest(monkeypatch):
    import asyncio

    from kyraan.agents import loop_tools
    monkeypatch.setattr(episodes, "recall", lambda chat_id, query, k: [])
    result = asyncio.run(loop_tools._memory_recall(5, {"query": "never discussed"}, ""))
    assert result["found"] == 0 and "never invent" in result["note"]


def test_recall_executor_store_down_is_honest(monkeypatch):
    import asyncio

    from kyraan.agents import loop_tools
    from kyraan.control_plane import kernel

    def boom(chat_id, query, k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(episodes, "recall", boom)
    with pytest.raises(kernel.ToolFailed, match="unavailable"):
        asyncio.run(loop_tools._memory_recall(5, {"query": "topic"}, ""))


def _cloud_down(monkeypatch):
    from kyraan.model_router import router

    def boom(**kwargs):
        raise RuntimeError("cloud down")

    monkeypatch.setattr(router, "call", boom)


def test_tagging_total_failure_defaults_to_sensitive(monkeypatch):
    _cloud_down(monkeypatch)

    def boom(text):
        raise RuntimeError("model down")

    monkeypatch.setattr(episodes, "_tag_chat", boom)
    assert episodes.sensitivity_flags("anything") == ["sensitive"]


def test_cloud_failure_falls_back_to_local(monkeypatch):
    _cloud_down(monkeypatch)
    monkeypatch.setattr(episodes, "_tag_chat", lambda text: ["health"])
    assert episodes.sensitivity_flags("anything") == ["health"]


def test_non_cloud_ok_text_never_reaches_the_router(monkeypatch):
    from kyraan.model_router import router

    def forbidden(**kwargs):
        raise AssertionError("local_only text reached the cloud tagger")

    monkeypatch.setattr(router, "call", forbidden)
    monkeypatch.setattr(episodes, "_tag_chat", lambda text: [])
    assert episodes.sensitivity_flags("private", exposure="local_only") == []


def test_normalize_maps_paraphrases_and_drops_junk():
    # seen live across probes: nano's "finances"/"legal matters"/"private
    # family matters", qwen's "_sensitive", gemma's "grief"/"sensory"
    assert episodes.normalize_flags(
        ["finances", "legal matters", "_sensitive", "grief", "health",
         "sensory", "banana"]) == ["emotional", "health", "sensitive"]
