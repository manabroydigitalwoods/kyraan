"""Obsidian vault indexing + the unified memory.search (owner directive
2026-09-02): parsing, heading-aware chunks, precise registry-bounded
person linking, event dates, change/delete sync, and the fan-out merge."""
from datetime import date

import pytest

from kyraan.store import notes

NOTE = """---
title: Darjeeling plan
date: 2026-10-20
tags: [travel, family]
---
# Trip
Going with [[Ruma]] and Kiaan for 5 nights.

## Hotel
Mayfair looked good. Ganak Roy suggested the toy train.
#todo book tickets
"""


def test_parse_note_extracts_frontmatter_links_tags():
    p = notes.parse_note(NOTE, "trips/darjeeling.md")
    assert p["title"] == "Darjeeling plan"
    assert p["links"] == ["Ruma"]
    assert p["tags"] == ["family", "todo", "travel"]
    assert p["body"].startswith("# Trip")


def test_chunks_carry_their_heading_path():
    p = notes.parse_note(NOTE, "x.md")
    chunks = notes.chunk_note(p["body"])
    assert chunks[0].startswith("[Trip] Going with")
    assert any(c.startswith("[Trip > Hotel] Mayfair") for c in chunks)


def test_event_date_prefers_frontmatter_then_text():
    p = notes.parse_note(NOTE, "x.md")
    assert notes.event_date_of(p, 0) == date(2026, 10, 20)
    q = notes.parse_note("Met the doctor on 3 Sep 2026 about the rash.", "y.md")
    assert notes.event_date_of(q, 0) == date(2026, 9, 3)
    assert notes.event_date_of(notes.parse_note("no dates here", "z.md"), 0) is None


def test_people_are_registry_bounded(monkeypatch):
    from kyraan.store import persons
    monkeypatch.setattr(persons, "name_map", lambda: {
        "ruma": "ruma", "kiaan": "kiaan", "ganak roy": "ganak_roy",
        "dada": "ganak_roy", "owner": "owner", "manab": "owner"})
    p = notes.parse_note(NOTE, "x.md")
    assert notes.link_people(p) == ["ganak_roy", "kiaan", "ruma"]
    # "Mayfair" is not a person; "Manab" resolves to owner and is excluded
    q = notes.parse_note("Manab met Mayfair staff", "w.md")
    assert notes.link_people(q) == []


@pytest.mark.pg
def test_sync_indexes_changes_and_deletions(tmp_path, monkeypatch):
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    from kyraan.store import pg
    if not pg.available():
        pytest.skip("local Postgres container unreachable")
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    monkeypatch.setattr(notes.embed, "embed", lambda chunks: [None] * len(chunks))
    from kyraan.store import persons
    monkeypatch.setattr(persons, "name_map", lambda: {"ruma": "ruma"})
    monkeypatch.setenv("KYRAAN_VAULT_FOLDERS", "Kyraan,Personal")
    monkeypatch.setenv("KYRAAN_VAULT_LOCAL_ONLY", "Personal")
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "junk.md").write_text("# hidden")
    (vault / "Kyraan").mkdir(); (vault / "Work").mkdir(); (vault / "Personal").mkdir()
    (vault / "Work" / "contract.md").write_text("# Client contract\nNever indexed.")
    (vault / "Personal" / "diary.md").write_text("# Diary\nA private note about the day.")
    (vault / "Kyraan" / "a.md").write_text(NOTE)
    (vault / "Kyraan" / "b.md").write_text("# B\nA plain note about [[Ruma]] and tea.")
    with pg.connection() as conn:
        conn.execute("DELETE FROM document WHERE chat_id = 4343"); conn.commit()
    c1 = notes.sync(4343, vault)
    assert (c1["indexed"], c1["unchanged"], c1["removed"]) == (3, 0, 0)  # Work/ excluded
    with pg.connection() as conn:
        exp = dict(conn.execute("""SELECT source_path, exposure FROM document
                                   WHERE chat_id = 4343 AND suppressed_by = '{}'""").fetchall())
    assert "Work/contract.md" not in exp                  # §2: never indexed
    assert exp["Personal/diary.md"] == "local_only"       # personal: local tier only
    assert exp["Kyraan/a.md"] == "cloud_ok"
    c2 = notes.sync(4343, vault)
    assert (c2["indexed"], c2["unchanged"]) == (0, 3)     # hashes unchanged
    (vault / "Kyraan" / "b.md").write_text("# B\nRewritten note, no people.")
    (vault / "Kyraan" / "a.md").unlink()
    c3 = notes.sync(4343, vault)
    assert (c3["indexed"], c3["removed"]) == (1, 1)
    with pg.connection() as conn:
        live = conn.execute(
            """SELECT source_path, subject_persons, entities, event_date FROM document
               WHERE chat_id = 4343 AND suppressed_by = '{}' AND source_path LIKE 'Kyraan/%'""").fetchall()
        ghosts = conn.execute(
            """SELECT suppressed_by FROM document
               WHERE chat_id = 4343 AND suppressed_by <> '{}'""").fetchall()
    assert live == [("Kyraan/b.md", [], [], None)]         # new version, unlinked
    assert sorted(str(g[0][0]) for g in ghosts) == sorted([notes.DELETED, notes.SUPERSEDED])
    pg.reset_pool_for_tests()


async def test_unified_search_fans_out_and_labels(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.store import documents, episodes

    async def facts(chat_id, args, raw):
        return {"matches": ["- Kiaan was born 12 Oct 2025 (saved 2026-08-20)"]}
    monkeypatch.setattr(loop_tools, "_memory_search_facts", facts)
    monkeypatch.setattr(documents, "search", lambda cid, q, k=3, person="": [
        {"kind": "note", "caption": "Darjeeling plan", "date": "2026-09-02",
         "text": "[Trip] Going with Ruma and Kiaan"}])
    monkeypatch.setattr(episodes, "recall", lambda cid, q, k=5: [
        "[recalled from 2026-08-30] we talked about the trip"])
    out = await loop_tools._memory_search(7, {"query": "trip"}, "")
    assert out["facts"] and out["documents"][0].startswith("[note: Darjeeling plan")
    assert out["conversations"]
    monkeypatch.setattr(documents, "search", lambda *a, **k: [])
    monkeypatch.setattr(episodes, "recall", lambda *a, **k: [])
    async def no_facts(chat_id, args, raw):
        return {"matches": []}
    monkeypatch.setattr(loop_tools, "_memory_search_facts", no_facts)
    out = await loop_tools._memory_search(7, {"query": "zzz"}, "")
    assert "nothing anywhere matches" in out["note"]



def test_empty_allowlist_indexes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("KYRAAN_VAULT_FOLDERS", raising=False)
    (tmp_path / "x.md").write_text("# secret\ncontract text")
    out = notes.sync(4343, tmp_path)
    assert "never indexed whole" in out["error"]


PERSON_NOTE = """---
type: person
name: Rakesh Chakraborty
aliases: [Rakesh, Rocky]
relation: college friend
tags: [friend]
---
# Rakesh Chakraborty
Met at NIT in 2009. Lives in Bangalore, works at a fintech.
"""


def test_person_note_detection():
    p = notes.parse_note(PERSON_NOTE, "Kyraan/people/Rakesh.md")
    assert notes.is_person_note(p, "Kyraan/people/Rakesh.md")
    q = notes.parse_note("# Rakesh\nplain", "Kyraan/people/Rakesh.md")
    assert notes.is_person_note(q, "Kyraan/people/Rakesh.md")     # folder convention
    assert not notes.is_person_note(q, "Kyraan/trips/Rakesh.md")


@pytest.mark.pg
def test_person_note_registers_person_and_aliases(tmp_path, monkeypatch):
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    from kyraan.store import persons, pg
    if not pg.available():
        pytest.skip("local Postgres container unreachable")
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    persons._cache.clear()
    monkeypatch.setattr(notes.embed, "embed", lambda chunks: [None] * len(chunks))
    monkeypatch.setenv("KYRAAN_VAULT_FOLDERS", "Kyraan")
    vault = tmp_path / "vault"
    (vault / "Kyraan" / "people").mkdir(parents=True)
    (vault / "Kyraan" / "people" / "Rakesh Chakraborty.md").write_text(PERSON_NOTE)
    (vault / "Kyraan" / "trip.md").write_text("# Trip\nRocky is joining us in Goa.")
    with pg.connection() as conn:
        conn.execute("DELETE FROM document WHERE chat_id = 4444")
        conn.execute("DELETE FROM person WHERE id = 'rakesh_chakraborty'")
        conn.commit()
    notes.sync(4444, vault)
    persons._cache.clear()
    nm = persons.name_map()
    assert nm.get("rakesh_chakraborty") == "rakesh_chakraborty"
    assert nm.get("rocky") == "rakesh_chakraborty"          # alias registered
    with pg.connection() as conn:
        rows = dict(conn.execute("""SELECT source_path, subject_persons FROM document
                                    WHERE chat_id = 4444 AND suppressed_by = '{}'""").fetchall())
    assert "rakesh_chakraborty" in rows["Kyraan/people/Rakesh Chakraborty.md"]
    # another note naming him by alias now links (second pass sees the alias)
    notes.sync(4444, vault)
    pg.reset_pool_for_tests()
