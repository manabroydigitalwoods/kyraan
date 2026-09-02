"""One-off backfill (2026-09-02): documents indexed before the linking
model existed get persons (registry matching over caption + text) and
entities (local-tier extraction). Idempotent; re-runnable.

    .venv/bin/python scripts/backfill_document_links.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

from kyraan.store import documents, entities, pg  # noqa: E402


def main() -> int:
    with pg.connection() as conn:
        rows = conn.execute(
            """SELECT id, kind, caption, text, subject_persons, entities
               FROM document WHERE suppressed_by = '{}' AND kind <> 'note'
               ORDER BY created_at""").fetchall()
    reset = "--reset" in sys.argv
    linked = tagged = 0
    for doc_id, kind, caption, text, people, ents in rows:
        # persons from the CAPTION only — owner-authored; registry names
        # inside OCR text linked a cash memo to "dada" (2026-09-02)
        found = documents.caption_people(caption or "")
        merged = sorted(set(list(people or []) + found))
        new_ents = [] if reset else list(ents or [])
        if not new_ents:
            new_ents = entities.extract(text or "", hint=caption or "")
        changed = merged != sorted(people or []) or new_ents != list(ents or [])
        if changed:
            with pg.connection() as conn:
                conn.execute(
                    "UPDATE document SET subject_persons = %s, entities = %s WHERE id = %s",
                    (merged, new_ents, doc_id))
                conn.commit()
        linked += int(merged != sorted(people or []))
        tagged += int(bool(new_ents) and not ents)
        print(f"{kind:7s} {str(caption)[:38]:38s} people={merged} entities={new_ents[:5]}")
    print(f"\n{len(rows)} documents: {linked} gained people, {tagged} gained entities")
    return 0


if __name__ == "__main__":
    sys.exit(main())
