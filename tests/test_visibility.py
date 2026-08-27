"""P3.5c — the §4 visibility matrix (every visibility row × every
viewer), fail-closed fallbacks, per-person extraction gating, and
review-queue routing. The Done-when: the matrix passes and the owner's
own flow is byte-identical."""
import os
from pathlib import Path

import pytest

from kyraan.control_plane import kernel
from kyraan.memory import engine

_env = Path(__file__).resolve().parents[1] / ".env"
if "KYRAAN_PG_DSN" not in os.environ and _env.exists():
    for line in _env.read_text().splitlines():
        if line.startswith("KYRAAN_PG_DSN="):
            os.environ["KYRAAN_PG_DSN"] = line.split("=", 1)[1].strip()
            break

from kyraan.store import pg  # noqa: E402

_PG_UP = pg.available()

# subject × visibility, one fact each — the §4 matrix rows
_MATRIX = [
    ("m1", "owner", "owner",        "Owner private fact"),
    ("m2", "owner", "shared",       "Owner shared fact"),
    ("m3", "ruma",  "owner",        "About Ruma, owner-visibility"),
    ("m4", "ruma",  "shared",       "About Ruma, shared"),
    ("m5", "ruma",  "subject_only", "Ruma's private fact"),
]


@pytest.fixture
def matrix_db(monkeypatch):
    if not _PG_UP:
        pytest.skip("local Postgres container unreachable")
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    monkeypatch.setenv("KYRAAN_MEMORY_BACKEND", "pg")
    pg.reset_pool_for_tests()
    from kyraan.store import facts
    with pg.connection() as conn:
        conn.execute("TRUNCATE person, fact CASCADE")
        facts.sync_entries(conn, [{
            "id": lid, "content": content, "target": f"people/{subject}.md",
            "kind": "other", "term": "long", "importance": "normal",
            "flags": [], "era": "current", "sphere": "personal",
            "created": "2026-08-27T00:00:00+00:00", "source": "t",
            "active": True, "superseded_by": None}
            for lid, subject, _vis, content in _MATRIX])
        conn.execute("INSERT INTO person (id, stage) VALUES ('ruma', 'read_mostly') "
                     "ON CONFLICT (id) DO UPDATE SET stage = 'read_mostly'")
        for lid, subject, vis, _content in _MATRIX:
            conn.execute(
                "UPDATE fact SET subject = %s, subject_reviewed = true, "
                "visibility = %s WHERE legacy_id = %s", (subject, vis, lid))
        conn.commit()
    yield
    pg.reset_pool_for_tests()


def _visible_to(person, stage):
    token = kernel.set_viewer(person, stage)
    try:
        context = engine.build_context("facts overview")
    finally:
        kernel.reset_viewer_stage(token)
    return {content for _lid, _s, _v, content in _MATRIX if content in context}


@pytest.mark.pg
def test_the_visibility_matrix(matrix_db):
    # owner: everything EXCEPT other people's subject_only facts
    assert _visible_to("owner", "owner") == {
        "Owner private fact", "Owner shared fact",
        "About Ruma, owner-visibility", "About Ruma, shared"}
    # ruma: shared facts + facts about herself, nothing else
    assert _visible_to("ruma", "read_mostly") == {
        "Owner shared fact", "About Ruma, owner-visibility",
        "About Ruma, shared", "Ruma's private fact"}
    # an unnamed non-owner viewer: shared only
    assert _visible_to("", "read_mostly") == {
        "Owner shared fact", "About Ruma, shared"}


@pytest.mark.pg
def test_owner_flow_is_byte_identical(matrix_db):
    # today's live store has no subject_only-of-others rows, so the new
    # owner clause must not change a single character of owner context
    token = kernel.set_viewer("owner", "owner")
    try:
        with_clause = engine.build_context("facts overview")
    finally:
        kernel.reset_viewer_stage(token)
    assert with_clause == engine.build_context("facts overview")  # default viewer IS owner


def test_non_owner_never_falls_back_to_files(monkeypatch):
    monkeypatch.setenv("KYRAAN_MEMORY_BACKEND", "pg")
    monkeypatch.setattr(engine, "_pg_candidates", lambda message: None)
    engine.add_fact("Owner secret in the file index", "preferences/s.md", "t")
    token = kernel.set_viewer("ruma", "read_mostly")
    try:
        assert engine.build_context("secret") == ""  # fail-closed: nothing
    finally:
        kernel.reset_viewer_stage(token)
    assert "Owner secret" in engine.build_context("secret")  # owner falls back


# --- extraction gating + review routing -----------------------------------

async def test_extraction_off_means_no_proposal(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.store import persons
    monkeypatch.setattr(persons, "extraction_enabled", lambda p: False)
    proposed = []
    monkeypatch.setattr(orchestrator, "_extraction_note",
                        lambda chat_id, text: proposed.append(text) or _instant(""))
    monkeypatch.setattr(orchestrator, "_dispatch", _instant_fn("hi Ruma"))
    token = kernel.set_viewer("ruma", "read_mostly")
    try:
        await orchestrator.handle_message(111, "my sister lives in Kolkata")
    finally:
        kernel.reset_viewer_stage(token)
    assert proposed == []  # her words never reached extraction


async def test_extraction_on_routes_to_her_queue(monkeypatch, tmp_path):
    from kyraan.memory import store as memory_store
    from kyraan.store import persons
    monkeypatch.setattr(persons, "extraction_enabled", lambda p: p == "ruma")
    token = kernel.set_viewer("ruma", "full")
    try:
        memory_store.propose_fact("people/ruma.md", "Sister lives in Kolkata",
                                  "my sister lives in Kolkata")
    finally:
        kernel.reset_viewer_stage(token)
    from kyraan.agents.review import _load_review_proposals
    assert _load_review_proposals("ruma")   # in HER queue
    assert not _load_review_proposals("owner")  # not the owner's


def test_legacy_proposals_belong_to_the_owner(tmp_path):
    from kyraan.agents.review import _load_review_proposals
    from kyraan.memory import store as memory_store
    (memory_store.PENDING_DIR / "20260101T000000Z-abc__people__x.md").write_text(
        "---\ntarget: people/x.md\nsource_statement: 'said'\n---\n\nLegacy fact\n")
    assert _load_review_proposals("owner")
    assert not _load_review_proposals("ruma")


def _instant(value):
    import asyncio
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


def _instant_fn(value):
    async def fn(*a, **k):
        return value
    return fn