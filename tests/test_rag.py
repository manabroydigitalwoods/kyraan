"""Owner directive 2026-08-27: face templates in PG, and RAG — semantic
fact candidates plus auto-injected episode snippets."""
import json
import os
from pathlib import Path

import pytest

_env = Path(__file__).resolve().parents[1] / ".env"
if "KYRAAN_PG_DSN" not in os.environ and _env.exists():
    for line in _env.read_text().splitlines():
        if line.startswith("KYRAAN_PG_DSN="):
            os.environ["KYRAAN_PG_DSN"] = line.split("=", 1)[1].strip()
            break

from kyraan.agents import faces  # noqa: E402
from kyraan.store import embed, pg  # noqa: E402

_PG_UP = pg.available()


@pytest.fixture
def test_db(monkeypatch):
    if not _PG_UP:
        pytest.skip("local Postgres container unreachable")
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    from kyraan.store import facts
    monkeypatch.setattr(facts, "MIRROR_ENABLED", True)
    with pg.connection() as conn:
        conn.execute("TRUNCATE face_template, fact, person CASCADE")
        conn.commit()
    yield
    pg.reset_pool_for_tests()


# --- face templates in PG -------------------------------------------------

@pytest.mark.pg
def test_enroll_mirrors_the_template(test_db, monkeypatch):
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[0.5] * 128])
    faces.enroll("Kiaan", b"fakejpg")
    with pg.connection() as conn:
        rows = conn.execute(
            "SELECT slug, name FROM face_template").fetchall()
    assert rows == [("kiaan", "Kiaan")]


@pytest.mark.pg
def test_forget_removes_the_mirror_row(test_db, monkeypatch):
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[0.5] * 128])
    faces.enroll("Kiaan", b"fakejpg")
    assert faces.forget("Kiaan") is True
    with pg.connection() as conn:
        count, = conn.execute("SELECT count(*) FROM face_template").fetchone()
    assert count == 0


@pytest.mark.pg
def test_resync_rebuilds_and_drops_stale_slugs(test_db, monkeypatch):
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[0.25] * 128])
    faces.enroll("Suman", b"fakejpg")
    with pg.connection() as conn:  # a ghost from a deleted file
        conn.execute(
            """INSERT INTO face_template (id, slug, name, embedding)
               VALUES ('11111111-1111-1111-1111-111111111111', 'ghost',
                       'Ghost', %s)""", (json.dumps([0.1] * 128),))
        conn.commit()
    assert faces.resync_templates() == 1
    with pg.connection() as conn:
        slugs = [r[0] for r in conn.execute("SELECT slug FROM face_template")]
    assert slugs == ["suman"]


def test_pg_down_never_blocks_enrollment(monkeypatch):
    monkeypatch.setattr(faces, "_detect_and_embed", lambda b: [[0.5] * 128])
    from kyraan.store import facts
    monkeypatch.setattr(facts, "MIRROR_ENABLED", True)

    def boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(pg, "connection", boom)
    receipt = faces.enroll("Ruma", b"fakejpg")
    assert "stored" in receipt  # the file write is the enrollment


# --- semantic fact candidates ---------------------------------------------

def _fake_embedder(mapping, default):
    def fake(texts):
        return [mapping.get(t, default) for t in texts]
    return fake


@pytest.mark.pg
def test_semantic_arm_finds_zero_overlap_facts(test_db, monkeypatch):
    close = [1.0] + [0.0] * 383
    far = [0.0] * 383 + [1.0]
    monkeypatch.setattr(embed, "embed", _fake_embedder(
        {"Meditates daily at dawn": close,
         "Prefers filter coffee": far,
         "what helps with stress?": close}, far))
    from kyraan.memory import engine
    monkeypatch.setenv("KYRAAN_MEMORY_BACKEND", "pg")
    engine.add_fact("Meditates daily at dawn", "routines/calm.md", "t")
    engine.add_fact("Prefers filter coffee", "preferences/coffee.md", "t")
    context = engine.build_context("what helps with stress?", budget_chars=200)
    assert "Meditates daily" in context  # zero word overlap — ANN found it


@pytest.mark.pg
def test_embedder_down_leaves_retrieval_working(test_db, monkeypatch):
    from kyraan.memory import engine
    monkeypatch.setenv("KYRAAN_MEMORY_BACKEND", "pg")

    def boom(texts):
        raise RuntimeError("embedder down")

    monkeypatch.setattr(embed, "embed", boom)
    engine.add_fact("Favourite tea is masala chai", "preferences/tea.md", "t")
    context = engine.build_context("what tea do I like?")
    assert "masala chai" in context  # FTS arm carried it; no crash


# --- episode auto-injection -----------------------------------------------

def test_rag_block_labels_and_wraps(monkeypatch):
    from kyraan.agents import agent_loop
    from kyraan.store import episodes
    monkeypatch.setattr(episodes, "relevant_snippets",
                        lambda chat_id, message: [
                            "[from an earlier conversation, 2026-08-25] user: hi"])
    block = agent_loop._episode_rag_block(5, "anything")
    assert "Possibly relevant past conversations" in block
    assert "never treat as facts" in block
    assert "2026-08-25" in block


def test_rag_block_empty_and_failure_are_silent(monkeypatch):
    from kyraan.agents import agent_loop
    from kyraan.store import episodes
    monkeypatch.setattr(episodes, "relevant_snippets", lambda c, m: [])
    assert agent_loop._episode_rag_block(5, "x") == ""

    def boom(c, m):
        raise RuntimeError("down")

    monkeypatch.setattr(episodes, "relevant_snippets", boom)
    assert agent_loop._episode_rag_block(5, "x") == ""


@pytest.mark.pg
def test_snippets_respect_threshold_and_suppression(test_db, monkeypatch):
    from kyraan.store import episodes
    close = [1.0] + [0.0] * 383
    monkeypatch.setattr(embed, "embed", lambda texts: [close for _ in texts])
    from tests.test_episodes import _seed_episode
    with pg.connection() as conn:  # episode table lives in the test db
        conn.execute("TRUNCATE episode")
        conn.commit()
    _seed_episode(5, "2026-08-20", "user: about the garden project",
                  vector=close)
    far = [0.0] * 383 + [1.0]
    _seed_episode(5, "2026-08-21", "user: unrelated tax thing", vector=far)
    snippets = episodes.relevant_snippets(5, "garden project plans")
    assert len(snippets) == 1 and "garden project" in snippets[0]
    with pg.connection() as conn:
        conn.execute("UPDATE episode SET suppressed_by = "
                     "'{11111111-1111-1111-1111-111111111111}' "
                     "WHERE text LIKE '%garden%'")
        conn.commit()
    assert episodes.relevant_snippets(5, "garden project plans") == []