"""P3.2a's Done-when checker: assert memory/index.json and the Postgres
fact table agree — every entry has a row, and content/active/
superseded_by match. Exits 1 with a diff on any mismatch.

    .venv/bin/python scripts/check_fact_sync.py
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
    entries = json.loads(index_path.read_text()) if index_path.exists() else []
    with pg.connection() as conn:
        rows = {r[0]: {"content": r[1], "active": r[2], "superseded_by": r[3]}
                for r in conn.execute(
                    """SELECT f.legacy_id, f.content, f.active, s.legacy_id
                       FROM fact f LEFT JOIN fact s ON s.id = f.superseded_by
                       WHERE f.legacy_id IS NOT NULL""")}
    problems = []
    for e in entries:
        row = rows.pop(e["id"], None)
        if row is None:
            problems.append(f"missing in PG: {e['id']} {e['content'][:60]!r}")
            continue
        for field, want in (("content", e["content"]),
                            ("active", bool(e["active"])),
                            ("superseded_by", e.get("superseded_by"))):
            if row[field] != want:
                problems.append(f"{e['id']} {field}: index={want!r} pg={row[field]!r}")
    for legacy_id, row in rows.items():
        problems.append(f"orphan row in PG (not in index): {legacy_id} "
                        f"{row['content'][:60]!r}")
    if problems:
        print(f"OUT OF SYNC ({len(problems)} problems):")
        for p in problems:
            print(f"  {p}")
        print("\nfix: .venv/bin/python scripts/resync_facts.py "
              "(orphans mean the index lost entries — investigate first)")
        return 1
    print(f"in sync: {len(entries)} index entries == {len(entries)} PG rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
