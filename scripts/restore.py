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
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(archive) as tar:
            member = next((m for m in tar.getmembers()
                           if m.name.endswith("data/pg_dump.sql")), None)
            if member is None:
                print("no pg_dump.sql in that backup (pre-PG archive?)")
                return 1
            tar.extract(member, tmp, filter="data")
            sql = (Path(tmp) / member.name).read_text()
    if target != "kyraan":
        subprocess.run(["docker", "exec", "kyraan-postgres", "psql", "-U", "kyraan",
                        "-c", f"DROP DATABASE IF EXISTS {target}"], capture_output=True)
        subprocess.run(["docker", "exec", "kyraan-postgres", "psql", "-U", "kyraan",
                        "-c", f"CREATE DATABASE {target}"], check=True, capture_output=True)
    restore = subprocess.run(
        ["docker", "exec", "-i", "kyraan-postgres", "psql", "-U", "kyraan", "-d", target],
        input=sql, capture_output=True, text=True)
    if restore.returncode != 0:
        print(f"restore FAILED: {restore.stderr[:300]}", file=sys.stderr)
        return 1
    count = subprocess.run(
        ["docker", "exec", "kyraan-postgres", "psql", "-U", "kyraan", "-d", target,
         "-tAc", "SELECT count(*) FROM pg_tables WHERE schemaname='public'"],
        capture_output=True, text=True)
    print(f"restored into {target!r}: {count.stdout.strip()} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
