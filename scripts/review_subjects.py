"""Owner review of fact SUBJECTS (P3.2a / audit P1): facts whose subject
could not be derived (people/ facts with no matching person row) sit at
subject='owner', subject_reviewed=false — and P3.5a's multi-user gate
stays closed while any remain.

    scripts/review_subjects.py                       # list unreviewed
    scripts/review_subjects.py --ok <legacy_id>...   # confirm: about the owner
    scripts/review_subjects.py --assign <legacy_id> <person_id>
        # fact is about <person_id> (person row created if missing)
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.store import pg  # noqa: E402


def list_unreviewed(conn) -> int:
    rows = conn.execute(
        """SELECT legacy_id, subject, content FROM fact
           WHERE NOT subject_reviewed ORDER BY created_at""").fetchall()
    if not rows:
        print("no facts awaiting subject review — P3.5a's gate is satisfiable")
        return 0
    print(f"{len(rows)} facts awaiting subject review:\n")
    for legacy_id, subject, content in rows:
        print(f"  {legacy_id}  [{subject}]  {content[:90]}")
    print("\nconfirm owner facts:  scripts/review_subjects.py --ok <id>...")
    print("reassign:             scripts/review_subjects.py --assign <id> <person>")
    return 0


def confirm(conn, legacy_ids: list) -> int:
    for lid in legacy_ids:
        n = conn.execute(
            "UPDATE fact SET subject_reviewed = true WHERE legacy_id = %s",
            (lid,)).rowcount
        print(f"  {lid}: {'confirmed as owner fact' if n else 'NOT FOUND'}")
    conn.commit()
    return 0


def assign(conn, legacy_id: str, person_id: str) -> int:
    person_id = person_id.strip().lower()
    conn.execute("INSERT INTO person (id, stage) VALUES (%s, 'none') "
                 "ON CONFLICT (id) DO NOTHING", (person_id,))
    n = conn.execute(
        """UPDATE fact SET subject = %s, subject_reviewed = true
           WHERE legacy_id = %s""", (person_id, legacy_id)).rowcount
    conn.commit()
    print(f"  {legacy_id}: {'assigned to ' + person_id if n else 'NOT FOUND'}")
    return 0 if n else 1


def main() -> int:
    args = sys.argv[1:]
    with pg.connection() as conn:
        if not args:
            return list_unreviewed(conn)
        if args[0] == "--ok" and len(args) > 1:
            return confirm(conn, args[1:])
        if args[0] == "--assign" and len(args) == 3:
            return assign(conn, args[1], args[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
