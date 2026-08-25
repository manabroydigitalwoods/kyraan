"""Phase 1 memory: plain Markdown files, no RAG/graph yet.

Design rule (Master Plan §6.1): extraction only saves what was *stated*,
never inferred, and every write is check-before-write. Concretely: writes
never touch the live memory/ tree directly — they land in
memory/pending_review/ as a proposed patch, and a human promotes or
discards them. `write_fact` never calls `promote`.
"""
from datetime import datetime, timezone
from pathlib import Path

MEMORY_ROOT = Path(__file__).resolve().parents[3] / "memory"
PENDING_DIR = MEMORY_ROOT / "pending_review"
PENDING_DIR.mkdir(exist_ok=True)


def read_fact_file(relative_path: str) -> str:
    """relative_path is e.g. 'people/wife.md' or 'routines/school_pickup.md'."""
    path = MEMORY_ROOT / relative_path
    if not path.exists():
        return ""
    return path.read_text()


def list_fact_files(category: str = "") -> list[str]:
    base = MEMORY_ROOT / category if category else MEMORY_ROOT
    if not base.exists():
        return []
    return sorted(
        str(p.relative_to(MEMORY_ROOT))
        for p in base.rglob("*.md")
        if PENDING_DIR not in p.parents
    )


def propose_fact(relative_path: str, content: str, source: str) -> Path:
    """Queue a fact for human review instead of writing memory directly.

    `source` should be the verbatim user statement the fact was extracted
    from, so a reviewer can check it was stated, not inferred.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = relative_path.replace("/", "__")
    proposal_path = PENDING_DIR / f"{ts}__{safe_name}"
    proposal_path.write_text(
        f"---\ntarget: {relative_path}\nsource_statement: {source!r}\n---\n\n{content}\n"
    )
    return proposal_path


def promote(proposal_path: Path) -> Path:
    """Human-approved: move a proposal into the live memory tree, appending
    to the target file if it already exists."""
    text = proposal_path.read_text()
    _, _, rest = text.partition("---\n")
    frontmatter, _, body = rest.partition("---\n")
    target_line = next(line for line in frontmatter.splitlines() if line.startswith("target:"))
    target_rel = target_line.split("target:", 1)[1].strip()

    target_path = MEMORY_ROOT / target_rel
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("a") as f:
        f.write(body.strip() + "\n\n")

    proposal_path.unlink()
    return target_path


def reject(proposal_path: Path) -> None:
    proposal_path.unlink()
