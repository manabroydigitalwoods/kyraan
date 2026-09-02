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

import os as _os

# KYRAAN_MEMORY_ROOT (owner, 2026-09-01): the memory tree is plain
# Markdown, so it can live inside an Obsidian vault — browse and edit
# facts as notes; the files stay the authority. Two owner-owned
# caveats, stated where the choice is made: (1) a vault synced by
# iCloud/Obsidian Sync sends these files off-machine — including
# local_only facts — under the OWNER'S sync, a deliberate trade;
# (2) external edits bypass the PG mirror until
# scripts/resync_facts.py (or the nightly job) reconciles.
MEMORY_ROOT = Path(
    _os.environ.get("KYRAAN_MEMORY_ROOT", "").strip()
    or Path(__file__).resolve().parents[3] / "memory")
PENDING_DIR = MEMORY_ROOT / "pending_review"
PENDING_DIR.mkdir(parents=True, exist_ok=True)  # a purged clean clone has no memory/ at all
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
    # P3.5c: proposals belong to whoever's turn produced them — their
    # review queue, not the owner's (review keyed by person, arch §4).
    from kyraan.control_plane import kernel as _kernel
    reviewer = _kernel.effective_reviewer()
    if reviewer is None:
        # Fail-closed (2026-08-28): an unidentified viewer's statement
        # must never enter ANY queue — least of all the owner's, which
        # is exactly how "User goes by the name Ruma" got in.
        raise ValueError("unidentified viewer — proposal refused")
    # P3.5e: in earned `sampled` mode, 2 of every 3 proposals carry a 24h
    # objection window instead of holding for review — still visible in
    # the pending queue exactly as today; a reject IS the objection.
    from kyraan.memory import review_scaling
    auto_line = ""
    if not review_scaling.next_proposal_holds():
        auto_line = f"auto_approve_after: {review_scaling.objection_deadline()}\n"
    with open(proposal_path, "x") as handle:
        handle.write(
            f"---\ntarget: {relative_path}\nsource_statement: {source!r}\n"
            f"reviewer: {reviewer}\n{auto_line}{meta_line}---\n\n{content}\n"
        )
    _os.chmod(proposal_path, 0o600)
    return proposal_path


def file_dispute(target: str, reviewer: str, old_id: str, new_id: str,
                 old_content: str, new_content: str) -> Path:
    """P3.5d: a cross-person contradiction as a resolvable queue item."""
    import uuid
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = PENDING_DIR / f"{ts}-{uuid.uuid4().hex[:6]}__dispute.md"
    meta = json.dumps({"old_id": old_id, "new_id": new_id})
    body = (f'DISPUTED: "{new_content}" (new) contradicts "{old_content}" '
            "(existing, from someone else). Approve = the new claim stands "
            "and supersedes; reject = the new claim is forgotten.")
    with open(path, "x") as handle:
        handle.write(f"---\ntarget: {target}\nreviewer: {reviewer}\n"
                     f"dispute: {meta}\n---\n\n{body}\n")
    _os.chmod(path, 0o600)
    return path


def dispute_meta(proposal_path: Path) -> dict | None:
    """The dispute ids when `proposal_path` is a dispute notice, else None."""
    try:
        text = proposal_path.read_text()
    except OSError:
        return None
    _, _, rest = text.partition("---\n")
    frontmatter, _, _ = rest.partition("---\n")
    line = next((ln for ln in frontmatter.splitlines()
                 if ln.startswith("dispute:")), None)
    if line is None:
        return None
    try:
        return json.loads(line.split("dispute:", 1)[1].strip())
    except json.JSONDecodeError:
        return None


def resolve_dispute(proposal_path: Path, keep_new: bool) -> str:
    """The reviewer's decision on a dispute notice: approve keeps the new
    claim (old superseded under THIS reviewer's authority), reject
    forgets the new claim. Both clear the disputed flags."""
    meta = dispute_meta(proposal_path)
    if not meta:
        raise ValueError("not a dispute notice")
    from kyraan.memory import engine
    old_id, new_id = meta["old_id"], meta["new_id"]
    if keep_new:
        engine.consolidate(new_id, [old_id])
        outcome = "the new claim stands"
    else:
        engine.forget([new_id])
        outcome = "the new claim was discarded"
    engine.clear_flag([old_id, new_id], "disputed")
    proposal_path.unlink(missing_ok=True)
    return outcome


def promote(proposal_path: Path, human: bool = True) -> Path:
    """Approved: move a proposal into the live memory tree, appending
    to the target file if it already exists, and register it with the
    engine index (classification + supersession). `human=False` is the
    P3.5e auto-approval sweep — it never touches the trust counters."""
    if dispute_meta(proposal_path) is not None:
        raise ValueError(
            "this is a DISPUTE notice, not a fact proposal — resolve it via "
            "the chat review flow (approve/reject), not promote")
    if human:
        from kyraan.memory import review_scaling
        review_scaling.record_decision(approved=True)
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
    if dispute_meta(proposal_path) is None:  # dispute resolutions don't count
        from kyraan.memory import review_scaling
        review_scaling.record_decision(approved=False)
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


def load_pending_facts(max_chars: int = 1500, reviewer: str = "owner") -> str:
    """Fact lines awaiting review — conversationally usable (the user
    stated them) while the live tree still requires the owner's promote.
    Found live: "who is deven?" failed although the fact sat in the queue.

    Keyed by REVIEWER (multi-user audit 2026-08-27): a viewer's prompt
    carries only THEIR queue — the owner's pending facts must never ride
    into another person's local-tier prompt."""
    lines = []
    for proposal in sorted(PENDING_DIR.glob("*.md")):
        text = proposal.read_text()
        _, _, rest = text.partition("---\n")
        frontmatter, _, body = rest.partition("---\n")
        owned_by = next((ln.split("reviewer:", 1)[1].strip()
                         for ln in frontmatter.splitlines()
                         if ln.startswith("reviewer:")), "owner")
        if owned_by != reviewer:
            continue
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
