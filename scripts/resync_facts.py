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
    # P3.3d: re-sweep for every FORGOTTEN fact — inactive with no
    # supersessor (an update is not a forget) and long-term (a short-term
    # expiry is not a forget either; the index can't distinguish an
    # expired short from a forgotten one, so shorts only sweep at
    # forget time). Catches forget-time sweeps deferred by a PG outage
    # so a forgotten topic can't linger findable. Idempotent.
    from kyraan.store import episodes
    swept = 0
    for entry in entries:
        if (not entry.get("active") and not entry.get("superseded_by")
                and entry.get("term") != "short"):
            swept += episodes.suppress_for_fact(
                facts.fact_uuid(entry["id"]), entry["content"])
    print(f"forget-cascade re-sweep: {swept} episode suppressions added")
    # P3.6a: graph catch-up — extract triples for any active fact that
    # has none (promote-time extraction is fire-and-forget; this is the
    # self-heal). Local cheap-tier model; idempotent.
    from kyraan.store import triples
    missing = triples.facts_missing_triples()
    extracted = 0
    for legacy_id, content in missing:
        try:
            extracted += triples.extract_and_store(legacy_id, content)
        except Exception as exc:
            print(f"  triple extraction failed for {legacy_id}: {exc}")
    with pg.connection() as conn:
        total_triples, = conn.execute("SELECT count(*) FROM triple").fetchone()
    print(f"graph: extracted for {len(missing)} facts (+{extracted} triples); "
          f"triple table holds {total_triples}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
