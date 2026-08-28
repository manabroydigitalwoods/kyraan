"""Restore a nightly backup's Postgres dump — into the live database, or
a scratch one for drills. A backup nobody has restored is a hope, not a
backup (workplan P3.0b).

    .venv/bin/python scripts/restore.py <backup.tar.gz> [--target kyraan_restore_test]
"""
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    archive = Path(sys.argv[1]).expanduser()
    target = sys.argv[sys.argv.index("--target") + 1] if "--target" in sys.argv else "kyraan"
    # The target is interpolated into DROP/CREATE DATABASE statements —
    # an unsanitized value is SQL injection AND a live-guard bypass
    # ("kyraan " passes the != check, psql still hits the live db)
    # (Bugbot P1, 2026-08-28). Identifier charset only, normalized
    # BEFORE the guard compares.
    import re
    target = target.strip().lower()
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", target):
        print(f"invalid --target {target!r}: database identifier "
              "characters only ([a-z_][a-z0-9_]*)", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(archive) as tar:
            member = next((m for m in tar.getmembers()
                           if m.name.endswith("data/pg_dump.sql")), None)
            if member is None:
                print("no pg_dump.sql in that backup (pre-PG archive?)")
                return 1
            tar.extract(member, tmp, filter="data")
            sql = (Path(tmp) / member.name).read_text()
    if target == "kyraan":
        # The live database is POPULATED: replaying a dump over it leaves
        # a merge of old and restored rows, and "success" would be a lie.
        # A real restore drops first, and that is destructive enough to
        # demand an explicit flag (Bugbot P1).
        if "--force-live" not in sys.argv:
            print("refusing to restore over the LIVE database.\n"
                  "  drill:   --target kyraan_restore_test\n"
                  "  for real: stop the bot, then re-run with --force-live",
                  file=sys.stderr)
            return 2
        print("restoring over the LIVE database (--force-live)", file=sys.stderr)
    # Fresh, empty target either way — restore means REPLACE, never merge.
    subprocess.run(["docker", "exec", "kyraan-postgres", "psql", "-U", "kyraan",
                    "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                    "-c", f"DROP DATABASE IF EXISTS {target} WITH (FORCE)"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["docker", "exec", "kyraan-postgres", "psql", "-U", "kyraan",
                    "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                    "-c", f"CREATE DATABASE {target}"],
                   check=True, capture_output=True, text=True)
    # ON_ERROR_STOP: without it psql reports success after skipping every
    # failed statement — the exact way a restore "passes" while restoring
    # nothing (Bugbot P1).
    restore = subprocess.run(
        ["docker", "exec", "-i", "kyraan-postgres", "psql", "-U", "kyraan",
         "-v", "ON_ERROR_STOP=1", "-d", target],
        input=sql, capture_output=True, text=True)
    if restore.returncode != 0:
        print(f"restore FAILED: {restore.stderr[:300]}", file=sys.stderr)
        return 1
    count = subprocess.run(
        ["docker", "exec", "kyraan-postgres", "psql", "-U", "kyraan", "-d", target,
         "-tAc", "SELECT count(*) FROM pg_tables WHERE schemaname='public'"],
        capture_output=True, text=True)
    tables = count.stdout.strip()
    if not tables.isdigit() or int(tables) == 0:
        # An empty database is a FAILED restore, however quiet psql was.
        print(f"restore produced NO tables in {target!r} — treating as failure",
              file=sys.stderr)
        return 1
    # Tables alone prove nothing: the 2026-08-28 drill restored 5 tables
    # and ZERO rows from a dump taken before the data existed, and this
    # script called it success. A backup you cannot restore DATA from is
    # the thing the drill exists to catch, so count rows too.
    rows = subprocess.run(
        ["docker", "exec", "kyraan-postgres", "psql", "-U", "kyraan", "-d", target,
         # schema_version is BOOKKEEPING: it holds a row per applied
        # migration even in a database that never held a single fact, so
        # counting it made a dataless dump look alive ("~1 rows", exit 0
        # — caught while testing this very check).
        "-tAc", """SELECT coalesce(sum(n_live_tup), 0)
                     FROM pg_stat_user_tables
                    WHERE relname <> 'schema_version'"""],
        capture_output=True, text=True).stdout.strip()
    total_rows = rows.split("|")[0].strip() if "|" in rows else rows
    print(f"restored into {target!r}: {tables} tables, ~{total_rows} content rows")
    if total_rows.isdigit() and int(total_rows) == 0:
        print("  FAILED: schema restored but NO CONTENT ROWS — the dump "
              "predates the data, or the backup ran against an empty "
              "database. This is not a usable backup.", file=sys.stderr)
        return 1
    print(f"  verify: docker exec kyraan-postgres psql -U kyraan -d {target} "
          "-c 'SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY 1'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
