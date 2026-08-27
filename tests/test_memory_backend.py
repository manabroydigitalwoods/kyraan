"""P3.2b — KYRAAN_MEMORY_BACKEND routing: default files, pg pulls
candidates from Postgres, pg-down falls back to files with one logged
event. Parity itself is proven by scripts/compare_memory_backends.py."""
from kyraan.memory import engine


def _seed_file_fact():
    return engine.add_fact("Likes filter coffee", "preferences/coffee.md", "test")


def test_default_backend_never_touches_pg(monkeypatch):
    monkeypatch.delenv("KYRAAN_MEMORY_BACKEND", raising=False)

    def boom(message):
        raise AssertionError("files mode must not call _pg_candidates")

    monkeypatch.setattr(engine, "_pg_candidates", boom)
    _seed_file_fact()
    assert "filter coffee" in engine.build_context("coffee?")


def test_pg_backend_uses_pg_candidates(monkeypatch):
    monkeypatch.setenv("KYRAAN_MEMORY_BACKEND", "pg")
    canned = [{"id": "x1", "content": "Came from Postgres", "target": "preferences/x.md",
               "kind": "preference", "term": "long", "importance": "normal",
               "flags": [], "era": "current", "sphere": "personal",
               "created": "2026-08-27T00:00:00+00:00", "active": True,
               "superseded_by": None}]
    monkeypatch.setattr(engine, "_pg_candidates", lambda message: canned)
    _seed_file_fact()  # present in files, must NOT be used
    context = engine.build_context("anything")
    assert "Came from Postgres" in context
    assert "filter coffee" not in context


def test_pg_down_falls_back_to_files_with_one_event(monkeypatch):
    monkeypatch.setenv("KYRAAN_MEMORY_BACKEND", "pg")
    from kyraan.store import pg as pg_module

    def boom():
        raise RuntimeError("pool timeout")

    monkeypatch.setattr(pg_module, "connection", boom)
    events = []
    monkeypatch.setattr(engine, "log_event",
                        lambda name, **kw: events.append(name))
    _seed_file_fact()
    assert "filter coffee" in engine.build_context("coffee?")
    assert events.count("memory_backend_fallback") == 1
