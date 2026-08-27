"""P3.6 — the relationship graph: extraction hygiene, exposure routing,
provenance storage, the forget cascade (read-side: a relation is served
only while an ACTIVE fact supports it), and the promote hook."""
import os
import threading
from pathlib import Path

import pytest

from kyraan.store import triples

_env = Path(__file__).resolve().parents[1] / ".env"
if "KYRAAN_PG_DSN" not in os.environ and _env.exists():
    for line in _env.read_text().splitlines():
        if line.startswith("KYRAAN_PG_DSN="):
            os.environ["KYRAAN_PG_DSN"] = line.split("=", 1)[1].strip()
            break

from kyraan.store import pg  # noqa: E402

_PG_UP = pg.available()


class _FakeResponse:
    def __init__(self, text):
        self.text = text


def test_extraction_drops_self_loops_and_relation_echoes(monkeypatch):
    from kyraan.model_router import router
    monkeypatch.setattr(router, "call", lambda **kw: _FakeResponse(
        '{"triples": ['
        '{"head":"owner","relation":"started_smoking","tail":"smoking"},'
        '{"head":"tomi","relation":"named","tail":"tomi"},'
        '{"head":"ruma","relation":"wife_of","tail":"owner"}]}'))
    assert triples.extract_triples("x") == [("ruma", "wife_of", "owner")]


def test_exposure_routes_the_tier(monkeypatch):
    from kyraan.model_router import router
    tiers = []

    def fake_call(**kwargs):
        tiers.append(kwargs["tier"])
        return _FakeResponse('{"triples": []}')

    monkeypatch.setattr(router, "call", fake_call)
    triples.extract_triples("x", exposure="cloud_ok")
    triples.extract_triples("x", exposure="local_only")
    assert tiers == ["frontier", "cheap"]


@pytest.fixture
def graph_db(monkeypatch):
    if not _PG_UP:
        pytest.skip("local Postgres container unreachable")
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    with pg.connection() as conn:
        conn.execute("TRUNCATE triple, fact, person CASCADE")
        conn.commit()
    yield
    pg.reset_pool_for_tests()


def _seed_fact(legacy_id, content, active=True):
    from kyraan.store import facts
    with pg.connection() as conn:
        facts.sync_entries(conn, [{
            "id": legacy_id, "content": content, "target": "people/x.md",
            "kind": "relationship", "term": "long", "importance": "normal",
            "flags": [], "era": "current", "sphere": "personal",
            "created": "2026-08-27T00:00:00+00:00", "source": "t",
            "active": active, "superseded_by": None}])
        conn.commit()


@pytest.mark.pg
def test_store_and_read_with_provenance(graph_db):
    _seed_fact("fact0001", "Wife's name is Ruma")
    assert triples.store_triples("fact0001", [("ruma", "wife_of", "owner")]) == 1
    # idempotent per provenance
    assert triples.store_triples("fact0001", [("ruma", "wife_of", "owner")]) == 1
    rows = triples.relations_for("ruma")
    assert len(rows) == 1
    assert rows[0]["relation"] == "wife_of"
    assert rows[0]["sources"] == ["Wife's name is Ruma"]
    with pg.connection() as conn:
        count, = conn.execute("SELECT count(*) FROM triple").fetchone()
    assert count == 1


@pytest.mark.pg
def test_missing_fact_stores_nothing(graph_db):
    assert triples.store_triples("nosuchid", [("a", "b", "c")]) == 0


@pytest.mark.pg
def test_forget_cascade_read_side(graph_db):
    _seed_fact("fact0002", "Wife's name is Ruma")
    triples.store_triples("fact0002", [("ruma", "wife_of", "owner")])
    assert triples.relations_for("ruma")
    _seed_fact("fact0002", "Wife's name is Ruma", active=False)  # forgotten
    assert triples.relations_for("ruma") == []  # relation gone, audit kept


def test_relations_executor_formats_with_citation(monkeypatch):
    import asyncio

    from kyraan.agents import loop_tools
    monkeypatch.setattr(triples, "relations_for", lambda name: [
        {"head": "ruma", "relation": "wife_of", "tail": "owner",
         "sources": ["Wife's name is Ruma"]}])
    result = asyncio.run(loop_tools._memory_relations(5, {"name": "Ruma"}, ""))
    assert result == ['ruma —wife_of→ owner (from: "Wife\'s name is Ruma")']


def test_relations_executor_empty_is_honest(monkeypatch):
    import asyncio

    from kyraan.agents import loop_tools
    monkeypatch.setattr(triples, "relations_for", lambda name: [])
    result = asyncio.run(loop_tools._memory_relations(5, {"name": "Zed"}, ""))
    assert result["found"] == 0 and "never invent" in result["note"]


def test_promote_hook_fires_extraction(monkeypatch):
    from kyraan.memory import engine
    from kyraan.store import facts
    monkeypatch.setattr(facts, "MIRROR_ENABLED", True)
    monkeypatch.setattr(facts, "mirror_entries", lambda entries: True)
    done = threading.Event()
    seen = {}

    def fake_extract(fact_id, content, exposure="cloud_ok"):
        seen.update(fact_id=fact_id, content=content)
        done.set()
        return 1

    monkeypatch.setattr(triples, "extract_and_store", fake_extract)
    fid = engine.add_fact("Sister's name is Mina", "people/mina.md", "test")
    assert done.wait(timeout=5), "the promote hook never ran"
    assert seen == {"fact_id": fid, "content": "Sister's name is Mina"}
