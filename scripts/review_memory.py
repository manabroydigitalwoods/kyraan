"""Human review CLI for the memory loop's check-before-write gate.

Extraction queues proposed facts into memory/pending_review/; nothing goes
live until promoted here. Usage:

    python scripts/review_memory.py                    # list pending proposals
    python scripts/review_memory.py promote <file|all>
    python scripts/review_memory.py reject <file|all>
"""
import sys

from kyraan.memory import store


def _pending() -> list:
    return sorted(store.PENDING_DIR.glob("*.md"))


def list_pending() -> None:
    proposals = _pending()
    if not proposals:
        print("Nothing pending review.")
        return
    for p in proposals:
        print(f"── {p.name}")
        print(p.read_text().rstrip())
        target_line = next((l for l in p.read_text().splitlines() if l.startswith("target:")), "")
        target_rel = target_line.split("target:", 1)[1].strip() if target_line else ""
        existing = store.read_fact_file(target_rel).strip() if target_rel else ""
        if existing:
            print(f"   ┌ already in memory/{target_rel} — check for contradictions:")
            for line in existing.splitlines():
                print(f"   │ {line}")
        print()
    print(f"{len(proposals)} proposal(s). Promote/reject by filename, or 'all'.")


def _resolve(name: str) -> list:
    if name == "all":
        return _pending()
    path = store.PENDING_DIR / name
    if not path.exists():
        sys.exit(f"No such proposal: {name} (run with no args to list)")
    return [path]


def main() -> None:
    if len(sys.argv) == 1:
        list_pending()
        return
    if len(sys.argv) != 3 or sys.argv[1] not in ("promote", "reject"):
        sys.exit(__doc__)
    action, name = sys.argv[1], sys.argv[2]
    for path in _resolve(name):
        if action == "promote":
            target = store.promote(path)
            print(f"Promoted {path.name} -> {target.relative_to(store.MEMORY_ROOT.parent)}")
        else:
            store.reject(path)
            print(f"Rejected {path.name}")


if __name__ == "__main__":
    main()
