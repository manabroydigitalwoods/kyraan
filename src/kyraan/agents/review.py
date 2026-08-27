"""In-chat memory review: proposal loading and the deterministic
approve/reject decision parser — no model sits between a reviewer's
decision and a memory write. The stateful flow (_review_memory, the
session dict) stays in orchestrator.py.

P3.5c: the flow is keyed by PERSON, not hardcoded owner — each
proposal carries a `reviewer:` stamp (missing = legacy = owner), and
`_load_review_proposals(reviewer)` serves only that person's queue: a
fact extracted from the spouse's own messages routes to HER review."""
from kyraan.memory import store as memory_store


def _load_review_proposals(reviewer: str = "owner") -> list:
    items = []
    for path in sorted(memory_store.PENDING_DIR.glob("*.md")):
        text = path.read_text()
        _, _, rest = text.partition("---\n")
        frontmatter, _, body = rest.partition("---\n")
        target = next((line.split("target:", 1)[1].strip()
                       for line in frontmatter.splitlines() if line.startswith("target:")), "?")
        owned_by = next((line.split("reviewer:", 1)[1].strip()
                         for line in frontmatter.splitlines()
                         if line.startswith("reviewer:")), "owner")
        if owned_by != reviewer:
            continue
        items.append((path, target, body.strip().lstrip("- ").strip()))
    return items


def _parse_review_decision(text: str, count: int):
    """Deterministic approve/reject parsing — no model sits between the
    owner's decision and a memory write. Returns (approved, rejected)
    index lists, or None when the message isn't a review decision."""
    import re
    words = re.findall(r"[a-z]+|\d+", text.lower())
    if not words or words[0] not in ("approve", "promote", "confirm", "save",
                                     "keep", "reject", "remove", "discard"):
        return None
    mode = None
    approved: set = set()
    rejected: set = set()
    for w in words:
        if w in ("approve", "promote", "confirm", "save", "keep"):
            mode = "a"
        elif w in ("reject", "remove", "discard", "delete", "drop"):
            mode = "r"
        elif w == "all" and mode:
            (approved if mode == "a" else rejected).update(range(count))
        elif w.isdigit() and mode:
            i = int(w) - 1
            if 0 <= i < count:
                (approved if mode == "a" else rejected).add(i)
    if not approved and not rejected:
        return None
    approved -= rejected  # an index named on both sides stays unsaved
    return (sorted(approved), sorted(rejected))
