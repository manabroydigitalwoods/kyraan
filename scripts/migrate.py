"""Apply migrations/*.sql in order, once each — recorded in
schema_version. Rerunnable with no diff (P3.0b's Done-when).

    .venv/bin/python scripts/migrate.py
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.store import pg  # noqa: E402


def main() -> int:
    with pg.connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
            filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
        applied = {r[0] for r in conn.execute("SELECT filename FROM schema_version")}
        for path in sorted((REPO / "migrations").glob("*.sql")):
            if path.name in applied:
                print(f"already applied: {path.name}")
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_version (filename) VALUES (%s)", (path.name,))
            print(f"applied: {path.name}")
        conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
