"""Semantic memory dedup — scan with the frontier model, apply only
what the owner approves. Nothing is deleted: duplicates are superseded
(kept as history), and the graph follows automatically.

    scripts/consolidate_memory.py                # scan + show proposals
    scripts/consolidate_memory.py --apply-all    # scan, then apply every group
    scripts/consolidate_memory.py --apply <keep_id> <dup_id>...
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.memory import consolidate  # noqa: E402


def show(proposals: list) -> None:
    if not proposals:
        print("no duplicate groups proposed — the store is clean")
        return
    for n, p in enumerate(proposals, 1):
        print(f"\nGROUP {n}: {p['reason']}")
        print(f"  KEEP      {p['keep']}: {p['keep_content']}")
        for dup_id, content in p["duplicates"]:
            print(f"  supersede {dup_id}: {content}")
    print("\napply one:  scripts/consolidate_memory.py --apply <keep_id> <dup_id>...")
    print("apply all:  scripts/consolidate_memory.py --apply-all")


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--apply"] and len(args) >= 3:
        gone = consolidate.apply(args[1], args[2:])
        for content in gone:
            print(f"superseded: {content}")
        return 0
    proposals = consolidate.scan()
    show(proposals)
    if args[:1] == ["--apply-all"]:
        for p in proposals:
            for content in consolidate.apply(p["keep"], [d for d, _ in p["duplicates"]]):
                print(f"superseded: {content}")
        print("\nall groups applied — re-run without flags to verify clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
