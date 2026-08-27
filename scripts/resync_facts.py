"""Full rebuild of the Postgres fact table from memory/index.json —
idempotent (identity is uuid5 of the legacy id), safe to run any time
sync drift is suspected. Owner-reviewed subjects in PG are preserved.

    .venv/bin/python scripts/resync_facts.py
"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.memory import engine  # noqa: E402
from kyraan.store import facts, pg  # noqa: E402


def main() -> int:
    index_path = engine._index_path()
    if not index_path.exists():
        print(f"no index at {index_path} — nothing to sync")
        return 0
    entries = json.loads(index_path.read_text())
    with pg.connection() as conn:
        facts.sync_entries(conn, entries)
        conn.commit()
        total, unreviewed = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE NOT subject_reviewed) "
            "FROM fact").fetchone()
    print(f"synced {len(entries)} index entries — fact table now holds "
          f"{total} rows, {unreviewed} awaiting subject review "
          f"(scripts/review_subjects.py)")
    # The self-heals, shared with the nightly job: the P3.3d forget
    # re-sweep and the P3.6a graph catch-up.
    swept = engine.resweep_forgotten()
    print(f"forget-cascade re-sweep: {swept} episode suppressions added")
    from kyraan.store import triples
    extracted = triples.catch_up()
    with pg.connection() as conn:
        total_triples, = conn.execute("SELECT count(*) FROM triple").fetchone()
    print(f"graph catch-up: +{extracted} triples; table holds {total_triples}")
    # RAG backfill: embed active facts the mirror couldn't (embedder down
    # at write time, or rows older than migration 010).
    from kyraan.store import embed
    with pg.connection() as conn:
        missing = conn.execute(
            "SELECT id, content FROM fact WHERE active AND embedding IS NULL"
        ).fetchall()
        if missing:
            vectors = embed.embed([content for _id, content in missing])
            for (fact_id, _), vector in zip(missing, vectors):
                conn.execute("UPDATE fact SET embedding = %s WHERE id = %s",
                             (json.dumps(vector), fact_id))
            conn.commit()
    print(f"fact embeddings: backfilled {len(missing)}")
    # Face templates: full file→PG rebuild (biometric mirror, local PG).
    from kyraan.agents import faces
    mirrored = faces.resync_templates()
    print(f"face templates: {mirrored} embeddings mirrored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
