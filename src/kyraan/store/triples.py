"""Relationship graph (P3.6, arch §2.1): typed triples extracted from
APPROVED facts only, one row per supporting fact — the fact is the
provenance, and a relation is served only while an ACTIVE fact supports
it (forget deactivates the fact; reads filter on it, so the relation
disappears without destroying the audit trail).

Extraction runs on the LOCAL cheap tier (a fact may carry any exposure;
the local pass respects them all), fire-and-forget from the promote
path — scripts/resync_facts.py extracts for any active fact that has no
rows yet, so a missed hook self-heals on the next resync.
"""
import json
import uuid

from kyraan.control_plane.logging_setup import log_event
from kyraan.store import facts, pg

# Tests flip this off (conftest autouse): the promote hook fires a real
# daemon thread, and with only the MIRROR flag guarding it, pg-marked
# fact tests leaked router retries into the production event log.
EXTRACT_ENABLED = True

_EXTRACT_SYSTEM = """You extract explicit relationship triples from ONE saved fact
about a person's life. Reply ONLY with JSON:
{"triples": [{"head": "...", "relation": "...", "tail": "..."}]}
Rules:
- Only relations the fact EXPLICITLY states — never infer or guess.
- head/tail: short lowercase entity names of REAL people, pets, places,
  or organizations. Software, apps, accounts, projects, and habits are
  NOT people or pets — a fact about them gets an empty list unless it
  names a real place/org relation (works_at, lives_in).
- The fact's owner is "owner" when the fact is about them ("my", "I").
- relation: one lowercase snake_case verb phrase (wife_of, father_of,
  son_of, works_at, lives_in, born_on, named, has_pet).
- DIRECTION: "head relation tail" must read as a true sentence — head
  IS the relation OF tail. Examples, follow them exactly:
  "My father's name is Ganak" -> {"head":"ganak","relation":"father_of","tail":"owner"}
  "Wife's name is Ruma"       -> {"head":"ruma","relation":"wife_of","tail":"owner"}
  "My son Kiaan was born on 12-10-2025" ->
      [{"head":"kiaan","relation":"son_of","tail":"owner"},
       {"head":"kiaan","relation":"born_on","tail":"12-10-2025"}]
  "User lives in Pune"        -> {"head":"owner","relation":"lives_in","tail":"pune"}
  "User had a dog named Rex"  -> {"head":"owner","relation":"has_pet","tail":"rex"}
- head and tail must be DIFFERENT things; never restate the relation
  word as the tail.
- A plain preference, habit, or routine with no second entity: empty list.
No other keys, no prose."""


def extract_triples(content: str, exposure: str = "cloud_ok") -> list:
    """Extraction from one approved fact: nano for cloud_ok facts (the
    memory block already puts them in every frontier prompt — nothing
    new is exposed; qwen3:8b kept inventing relations like "has_pet
    telegram" through two prompt tightenings), the local cheap tier for
    any other exposure. Raises on failure — resync self-heals."""
    from kyraan.model_router import router
    tier = "frontier" if exposure == "cloud_ok" else "cheap"
    # 768: nano burns hidden reasoning tokens from the same budget and a
    # tight cap returns EMPTY text (seen live when the few-shots landed).
    response = router.call(prompt=content[:1000], system=_EXTRACT_SYSTEM,
                           tier=tier, force_json=True, max_tokens=768)
    rows = json.loads(router.strip_code_fence(response.text)).get("triples") or []
    import re as _re

    def _slug(value: str) -> str:
        # one canonical spelling per entity: nano's run-to-run variance
        # produced "ganak roy" AND "ganak_roy" rows (gap audit)
        return _re.sub(r"[\s,]+", "_", str(value).strip().lower())[:80]

    clean = []
    for row in rows:
        head = _slug(row.get("head", ""))
        relation = _slug(row.get("relation", ""))
        tail = _slug(row.get("tail", ""))
        # Deterministic sanity, model-independent: a tail restating the
        # relation ("started_smoking"→"smoking") or a self-loop is noise.
        if not (head and relation and tail) or head == tail:
            continue
        if tail in relation or relation in tail:
            continue
        clean.append((head, relation, tail))
    return clean


def store_triples(fact_legacy_id: str, rows: list) -> int:
    fact_id = facts.fact_uuid(fact_legacy_id)
    written = 0
    with pg.connection() as conn:
        exists = conn.execute("SELECT 1 FROM fact WHERE id = %s",
                              (fact_id,)).fetchone()
        if not exists:
            return 0  # fact not mirrored yet; resync will catch up
        for head, relation, tail in rows:
            conn.execute(
                """INSERT INTO triple (id, head, relation, tail, fact_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (head, relation, tail, fact_id) DO NOTHING""",
                (str(uuid.uuid4()), head, relation, tail, fact_id))
            written += 1
        conn.commit()
    return written


def extract_and_store(fact_legacy_id: str, content: str,
                      exposure: str = "cloud_ok") -> int:
    rows = extract_triples(content, exposure)
    written = store_triples(fact_legacy_id, rows) if rows else 0
    with pg.connection() as conn:
        # Stamp even a zero-yield extraction — an unstamped routine fact
        # was re-sent to the model on every catch-up (gap audit).
        conn.execute("UPDATE fact SET triples_extracted_at = now() "
                     "WHERE legacy_id = %s", (fact_legacy_id,))
        conn.commit()
    log_event("triples_extracted", fact=fact_legacy_id, triples=written)
    return written


def facts_missing_triples() -> list:
    """(legacy_id, content) of active facts never yet extracted — the
    catch-up set (nightly + resync)."""
    with pg.connection() as conn:
        return conn.execute(
            """SELECT f.legacy_id, f.content FROM fact f
               WHERE f.active AND f.legacy_id IS NOT NULL
                     AND f.triples_extracted_at IS NULL""").fetchall()


def catch_up() -> int:
    """Extract for every active fact never yet extracted — the self-heal
    for promote-time threads that died (called nightly and by resync)."""
    extracted = 0
    for legacy_id, content in facts_missing_triples():
        try:
            extracted += extract_and_store(legacy_id, content)
        except Exception as exc:
            log_event("triple_extract_deferred", fact=legacy_id,
                      reason=str(exc)[:150])
    return extracted


def relations_for(name: str) -> list:
    """Every relation touching `name` (head or tail, case-insensitive),
    DISTINCT on (head, relation, tail), served only while an ACTIVE fact
    supports it — with the supporting facts as provenance."""
    like = f"%{name.strip().lower()}%"
    # Defense-in-depth (multi-user audit 2026-08-27): the graph tool is
    # already outside non-owner stage allowlists, but the DATA layer
    # enforces the §4 visibility clause too — a future allowlist edit
    # cannot leak a private fact's relation.
    from kyraan.control_plane import kernel
    viewer = kernel.viewer_person()
    if viewer == "owner":
        vis_sql = "AND NOT (f.visibility = 'subject_only' AND f.subject <> 'owner')"
        vis_params: tuple = ()
    else:
        vis_sql = "AND (f.visibility = 'shared' OR f.subject = %s)"
        vis_params = (viewer,)
    with pg.connection() as conn:
        rows = conn.execute(
            f"""SELECT t.head, t.relation, t.tail,
                      array_agg(DISTINCT f.content) AS sources
               FROM triple t JOIN fact f ON f.id = t.fact_id
               WHERE f.active {vis_sql}
                     AND (t.head LIKE %s OR t.tail LIKE %s)
               GROUP BY t.head, t.relation, t.tail
               ORDER BY t.head, t.relation, t.tail""",
            (*vis_params, like, like)).fetchall()
    return [{"head": h, "relation": r, "tail": t, "sources": list(s)}
            for h, r, t, s in rows]
