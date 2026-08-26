"""Phase 1 memory: plain Markdown files, no RAG/graph yet.

Design rule (Master Plan §6.1): extraction only saves what was *stated*,
never inferred, and every write is check-before-write. Concretely: writes
never touch the live memory/ tree directly — they land in
memory/pending_review/ as a proposed patch, and a human promotes or
discards them. `write_fact` never calls `promote`.
"""
import json
import re

from kyraan.control_plane.filelock import locked
from datetime import datetime, timezone
from pathlib import Path

import os as _os

MEMORY_ROOT = Path(__file__).resolve().parents[3] / "memory"
PENDING_DIR = MEMORY_ROOT / "pending_review"
PENDING_DIR.mkdir(exist_ok=True)
for _d in (MEMORY_ROOT, PENDING_DIR):
    try:
        _os.chmod(_d, 0o700)
    except OSError:
        pass

# Fact paths are constrained to a small shape on purpose: extraction output
# is model-generated, and an unvalidated target like "../../.env" would let
# promote() write outside the memory tree. Categories match the seeded
# layout; extend the alternation when a new category is deliberately added.
_ALLOWED_PATH = re.compile(r"^(people|routines|work|preferences)/[a-z0-9_-]+\.md$")


def _validate_path(relative_path: str) -> None:
    if not _ALLOWED_PATH.match(relative_path):
        raise ValueError(f"invalid memory path: {relative_path!r}")


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


def propose_fact(relative_path: str, content: str, source: str, meta: dict | None = None) -> Path:
    """Queue a fact for human review instead of writing memory directly.

    `source` should be the verbatim user statement the fact was extracted
    from, so a reviewer can check it was stated, not inferred. `meta`
    carries the extraction's classification (term/importance/flags/
    supersedes) for the engine index at promote time.
    """
    relative_path = relative_path.strip().lower()  # 'Preferences/x.md' == 'preferences/x.md' — seen live rejected for a capital P
    _validate_path(relative_path)
    import uuid
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = relative_path.replace("/", "__")
    # uuid suffix + exclusive create: two same-target facts in one second
    # silently overwrote each other (external review P1).
    proposal_path = PENDING_DIR / f"{ts}-{uuid.uuid4().hex[:6]}__{safe_name}"
    meta_line = f"meta: {json.dumps(meta, ensure_ascii=False)}\n" if meta else ""
    with open(proposal_path, "x") as handle:
        handle.write(
            f"---\ntarget: {relative_path}\nsource_statement: {source!r}\n{meta_line}---\n\n{content}\n"
        )
    _os.chmod(proposal_path, 0o600)
    return proposal_path


def promote(proposal_path: Path) -> Path:
    """Human-approved: move a proposal into the live memory tree, appending
    to the target file if it already exists, and register it with the
    engine index (classification + supersession)."""
    text = proposal_path.read_text()
    _, _, rest = text.partition("---\n")
    frontmatter, _, body = rest.partition("---\n")
    target_line = next(line for line in frontmatter.splitlines() if line.startswith("target:"))
    target_rel = target_line.split("target:", 1)[1].strip()
    meta = {}
    meta_line = next((line for line in frontmatter.splitlines() if line.startswith("meta:")), "")
    if meta_line:
        try:
            meta = json.loads(meta_line.split("meta:", 1)[1].strip())
        except json.JSONDecodeError:
            meta = {}
    # Re-validate at promote time too — a proposal file could have been
    # hand-edited between propose and promote.
    _validate_path(target_rel)

    # Order matters for retry safety (review P2): the ENGINE INDEX is the
    # retrieval authority and its add is idempotent, so it goes first; the
    # Markdown append is the human audit copy; the unlink is last. A crash
    # between steps leaves a retryable proposal, never a lost fact.
    from kyraan.memory import engine  # late: engine imports store
    engine.add_fact(
        content=body.strip(),
        target=target_rel,
        source=text[:200],
        kind=engine._KIND_BY_CATEGORY.get(target_rel.split("/", 1)[0], "other"),
        term=meta.get("term", "long"),
        importance=meta.get("importance", "normal"),
        flags=meta.get("flags") or (),
        supersedes=meta.get("supersedes"),
        era=meta.get("era", "current"),
        sphere=meta.get("sphere", "personal"),
    )

    target_path = MEMORY_ROOT / target_rel
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with locked(target_path):
        # Exact line-set comparison under the target's lock (round-4 P2:
        # substring matching suppressed distinct shorter facts, and the
        # unlocked check-then-append let concurrent promotes duplicate).
        existing_lines = set()
        if target_path.exists():
            existing_lines = {l.strip() for l in target_path.read_text().splitlines() if l.strip()}
        body_lines = [l.strip() for l in body.strip().splitlines() if l.strip()]
        if not all(l in existing_lines for l in body_lines):
            with target_path.open("a") as f:
                f.write(body.strip() + "\n\n")
            _os.chmod(target_path, 0o600)
            try:
                _os.chmod(target_path.parent, 0o700)
            except OSError:
                pass

    proposal_path.unlink()
    return target_path


def reject(proposal_path: Path) -> None:
    proposal_path.unlink()


def _fact_words(line: str) -> frozenset:
    return frozenset(
        w.strip(".,!?'\"").casefold() for w in line.strip().lstrip("-").split()
        if len(w.strip(".,!?'\"")) > 2 and w.casefold() not in ("my", "the", "our", "his", "her")
    )


def known_fact_lines() -> list:
    """Word-sets of every fact line, live AND pending — the dedup baseline.
    Word-set containment, not exact match: live testing showed "my wife's
    name is Mira" (model) vs "Wife's name is Mira" (stored) defeating
    string equality."""
    sets = []
    for rel in list_fact_files():
        if rel == "README.md":
            continue
        for line in read_fact_file(rel).splitlines():
            if line.strip().startswith("-"):
                sets.append(_fact_words(line))
    for proposal in PENDING_DIR.glob("*.md"):
        _, _, rest = proposal.read_text().partition("---\n")
        _, _, body = rest.partition("---\n")
        for line in body.splitlines():
            if line.strip().startswith("-"):
                sets.append(_fact_words(line))
    return [s for s in sets if len(s) >= 2]


def is_known_fact(content: str, known: list) -> bool:
    words = _fact_words(content)
    if len(words) < 2:
        return False
    return any(words <= existing or existing <= words for existing in known)


def load_pending_facts_filtered(max_chars: int = 1500) -> str:
    """Pending facts SAFE for a cloud prompt (security round 2, P1): a
    proposal rides along only when its extraction meta exists and carries
    no discretion flags — unclassified or sensitive/emotional proposals
    stay machine-local until the owner reviews them."""
    lines = []
    for proposal in sorted(PENDING_DIR.glob("*.md")):
        text = proposal.read_text()
        _, _, rest = text.partition("---\n")
        frontmatter, _, body = rest.partition("---\n")
        meta_line = next((l for l in frontmatter.splitlines() if l.startswith("meta:")), "")
        if not meta_line:
            continue  # pre-classification proposal: conservative exclusion
        try:
            meta = json.loads(meta_line.split("meta:", 1)[1].strip())
        except json.JSONDecodeError:
            continue
        if set(meta.get("flags") or []) & {"sensitive", "emotional"}:
            continue
        lines.extend(l for l in body.splitlines() if l.strip().startswith("-"))
    return "\n".join(lines)[:max_chars]


def load_pending_facts(max_chars: int = 1500) -> str:
    """Fact lines awaiting review — conversationally usable (the user
    stated them) while the live tree still requires the owner's promote.
    Found live: "who is biren?" failed although the fact sat in the queue."""
    lines = []
    for proposal in sorted(PENDING_DIR.glob("*.md")):
        _, _, rest = proposal.read_text().partition("---\n")
        _, _, body = rest.partition("---\n")
        lines.extend(l for l in body.splitlines() if l.strip().startswith("-"))
    text = "\n".join(lines)
    return text[:max_chars]


def load_all_facts(max_chars: int = 4000) -> str:
    """Every live fact, formatted for direct inclusion in a system prompt.

    Phase 1 memory is a handful of small MD files, so "retrieval" is just
    reading all of them — no RAG until the tree outgrows the prompt budget
    (the cap makes that failure mode a visible truncation, not a crash).
    """
    sections = []
    for rel in list_fact_files():
        if rel == "README.md" or rel.startswith("pending_review/"):
            continue
        content = read_fact_file(rel).strip()
        if content:
            sections.append(f"### {rel}\n{content}")
    text = "\n\n".join(sections)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[...memory truncated — tree has outgrown the prompt budget]"
    return text
