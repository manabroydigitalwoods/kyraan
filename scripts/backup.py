"""Nightly state backup — the "one disk hiccup loses real state" hole.

    .venv/bin/python scripts/backup.py

Tars data/ (face templates, memory index, reminders, tasks, cost ledger,
session summaries), memory/ (the fact tree), config/permissions.yaml and
.env into BACKUP_DIR (default ~/Backups/kyraan), keeps the newest 14,
prunes the rest. Local by design — point KYRAAN_BACKUP_DIR at an
external disk or iCloud folder to get off-machine copies (governance §0:
backups leave the machine only by the owner's explicit choice of
destination). Runs nightly at 03:30 via the ai.kyraan.backup launchd
agent; safe to run by hand anytime.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    dest = Path(os.environ.get("KYRAAN_BACKUP_DIR", "") or Path.home() / "Backups" / "kyraan")
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)  # .env rides inside — owner-only, always
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = dest / f"kyraan-{stamp}.tar.gz"
    members = [m for m in ("data", "memory", "config/permissions.yaml", ".env")
               if (REPO / m).exists()]
    result = subprocess.run(
        ["tar", "-czf", str(target), "-C", str(REPO), *members],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(f"backup FAILED: {result.stderr[:300]}", file=sys.stderr)
        return 1
    os.chmod(target, 0o600)
    backups = sorted(dest.glob("kyraan-*.tar.gz"))
    for old in backups[:-14]:
        old.unlink()
    size_mb = target.stat().st_size / 1e6
    print(f"backup ok: {target.name} ({size_mb:.1f} MB), {min(len(backups), 14)} kept in {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
