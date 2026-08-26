"""The memory engine — classification, supersession, and intelligent
retrieval over the fact store.

The flat "dump every fact line into every prompt" approach broke down the
day memory got real: contradictions accumulated (two father names live at
once), semantic duplicates slipped word-set dedup, nothing distinguished
an emergency-relevant fact from small talk, and growth past the char cap
would silently truncate arbitrary facts.

The engine adds a JSON index over the human-readable MD tree (the tree
stays the audit log; the index is the retrieval authority):
- every fact carries kind, term (long/short), importance
  (critical/high/normal), and safety flags (health, safety, emergency,
  danger)
- a new fact can SUPERSEDE an old one — the old entry deactivates instead
  of contradicting forever
- retrieval is budgeted and prioritized: safety-critical and identity
  facts ALWAYS ride along; the rest are scored by relevance to the
  current message and recency
- short-term facts expire (14 days) instead of polluting forever
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from kyraan.control_plane.logging_setup import log_event
from kyraan.memory import store
from kyraan.control_plane.filelock import locked

INDEX_NAME = "index.json"

_KIND_BY_CATEGORY = {"people": "relationship", "routines": "routine",
                     "work": "work", "preferences": "preference"}
_SHORT_TERM_DAYS = 14
_VALID_TERM = {"long", "short"}
_VALID_IMPORTANCE = {"critical", "high", "normal"}
_VALID_FLAGS = {"health", "safety", "emergency", "danger",
                "fun", "sentimental", "milestone",
                "emotional", "sensitive"}
_SAFETY_FLAGS = {"health", "safety", "emergency", "danger"}
# Discretion flags change BEHAVIOR, not just rank: these facts surface
# only on strong, direct relevance — never volunteered into unrelated
# answers — and carry their tag so the model answers with care.
_DISCRETION_FLAGS = {"emotional", "sensitive"}
_VALID_ERA = {"current", "past"}
_VALID_SPHERE = {"personal", "work", "both"}
# Words in a message that mean the user is reaching for the past — old
# memories rank up instead of down.
_PAST_CUES = {"used", "before", "back", "then", "old", "earlier", "remember",
              "when", "history", "past", "childhood", "younger"}
_VALID_KINDS = {"identity", "relationship", "preference", "routine",
                "work", "situational", "other"}


def _index_path():
    return store.MEMORY_ROOT / INDEX_NAME


def _load() -> list:
    try:
        return json.loads(_index_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(entries: list) -> None:
    _index_path().parent.mkdir(parents=True, exist_ok=True)
    _index_path().write_text(json.dumps(entries, indent=1, ensure_ascii=False))


def _words(text: str) -> set:
    words = set()
    for raw in text.split():
        w = raw.strip(".,!?'\"-—()").lower()
        if w.endswith("'s") or w.endswith("\u2019s"):
            w = w[:-2]
        if len(w) > 2:
            words.add(w)
    return words


def migrate_from_tree() -> int:
    """One-time backfill: every live fact line becomes an index entry with
    conservative defaults (long-term, normal importance, kind from its
    category). Existing installs keep working the moment the engine lands."""
    if _index_path().exists():
        return 0
    entries = []
    seen = set()
    for rel in store.list_fact_files():
        if "/" not in rel:
            continue  # README and any other root file — docs, not facts
        category = rel.split("/", 1)[0]
        for line in store.read_fact_file(rel).splitlines():
            line = line.strip()
            if not line.startswith("-"):
                continue
            content = line.lstrip("- ").strip()
            key = frozenset(_words(content))
            if not content or key in seen:
                continue
            seen.add(key)
            entries.append({
                "id": uuid.uuid4().hex[:8],
                "content": content,
                "target": rel,
                "kind": _KIND_BY_CATEGORY.get(category, "other"),
                "term": "long",
                "importance": "normal",
                "flags": [],
                "era": "current",
                "sphere": "work" if category == "work" else "personal",
                "created": datetime.now(timezone.utc).isoformat(),
                "source": "(migrated from tree)",
                "active": True,
                "superseded_by": None,
            })
    _save(entries)
    log_event("memory_index_migrated", entries=len(entries))
    return len(entries)


def add_fact(content: str, target: str, source: str, kind: str = "other",
             term: str = "long", importance: str = "normal",
             flags=(), supersedes: str | None = None,
             era: str = "current", sphere: str = "personal") -> str:
    """Register a promoted fact. `supersedes` (verbatim or near-verbatim
    text of an existing fact) deactivates the old entries — a rename stops
    being a contradiction and becomes history."""
    kind = kind if kind in _VALID_KINDS else "other"
    term = term if term in _VALID_TERM else "long"
    importance = importance if importance in _VALID_IMPORTANCE else "normal"
    flags = sorted(set(flags) & _VALID_FLAGS)
    new_id = uuid.uuid4().hex[:8]

    with locked(_index_path()):
        entries = _load()
        return _add_locked(entries, new_id, content, target, source, kind,
                           term, importance, flags, supersedes, era, sphere)


def _add_locked(entries, new_id, content, target, source, kind, term,
                importance, flags, supersedes, era, sphere) -> str:
    if supersedes:
        old_words = _words(supersedes)
        for entry in entries:
            if not entry["active"]:
                continue
            entry_words = _words(entry["content"])
            if entry_words and (entry_words <= old_words or old_words <= entry_words):
                entry["active"] = False
                entry["superseded_by"] = new_id
                log_event("memory_superseded", old=entry["content"][:80],
                          new=content[:80])

    entries.append({
        "id": new_id, "content": content.lstrip("- ").strip(), "target": target,
        "kind": kind, "term": term, "importance": importance, "flags": flags,
        "era": era if era in _VALID_ERA else "current",
        "sphere": sphere if sphere in _VALID_SPHERE else "personal",
        "created": datetime.now(timezone.utc).isoformat(), "source": source,
        "active": True, "superseded_by": None,
    })
    _save(entries)
    return new_id


def active_entries() -> list:
    """Live entries, with short-term expiry applied lazily."""
    entries = _load()
    now = datetime.now(timezone.utc)
    changed = False
    for entry in entries:
        if (entry["active"] and entry.get("term") == "short"):
            try:
                created = datetime.fromisoformat(entry["created"])
            except ValueError:
                continue
            if now - created > timedelta(days=_SHORT_TERM_DAYS):
                entry["active"] = False
                changed = True
                log_event("memory_short_term_expired", content=entry["content"][:80])
    if changed:
        _save(entries)
    return [e for e in entries if e["active"]]


def build_context(message: str = "", budget_chars: int = 3500) -> str:
    """The memory block for a prompt: safety-critical and identity facts
    ALWAYS included; the rest ranked by relevance to the current message,
    then importance, then recency — filled to budget, never blind-cut."""
    entries = active_entries()
    if not entries:
        return ""
    message_words = _words(message)

    reaching_for_past = bool({w.lower() for w in message.split()} & _PAST_CUES)
    always, ranked = [], []
    for entry in entries:
        safety = set(entry.get("flags") or []) & _SAFETY_FLAGS
        if safety or entry["importance"] == "critical" or entry["kind"] == "identity":
            always.append(entry)
            continue
        overlap = len(message_words & _words(entry["content"]))
        if (set(entry.get("flags") or []) & _DISCRETION_FLAGS) and overlap < 2:
            # A sensitive or emotional memory with no strong tie to the
            # current message stays private — discretion means absence,
            # not a lower rank.
            continue
        score = overlap * 10 + (5 if entry["importance"] == "high" else 0)
        # Old memories: normally quieter, but when the user reaches for
        # the past ("what did I use to..."), they lead. A past fact with
        # health/safety weight is in `always` above regardless — an
        # ex-smoker fact still matters to the present.
        if entry.get("era") == "past":
            score += 8 if reaching_for_past else -3
        # Fun/sentimental memories surface for reminiscing, stay out of
        # the way of operational asks.
        if {"fun", "sentimental", "milestone"} & set(entry.get("flags") or []):
            score += 4 if reaching_for_past else -2
        ranked.append((score, entry))
    # highest score first; newer entries break ties
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["created"]), reverse=False)

    lines, used = [], 0

    def fits(entry) -> bool:
        nonlocal used
        line = f"- {entry['content']}"
        if entry["flags"]:
            line += f"  [{'/'.join(entry['flags']).upper()}]"
        if used + len(line) > budget_chars:
            return False
        lines.append(line)
        used += len(line) + 1
        return True

    for entry in always:
        fits(entry)
    for _score, entry in ranked:
        if not fits(entry):
            break
    return "\n".join(lines)


def find_matches(text: str) -> list:
    """Active entries plausibly meant by `text` — word containment or a
    2+-word overlap. Deterministic: no model decides what gets forgotten."""
    wanted = _words(text)
    if not wanted:
        return []
    matches = []
    for entry in active_entries():
        entry_words = _words(entry["content"])
        overlap = wanted & entry_words
        if entry_words and (wanted <= entry_words or entry_words <= wanted or len(overlap) >= 2
                            or (len(wanted) == 1 and overlap)):
            matches.append(entry)
    return matches


def forget(entry_ids: list) -> list:
    """Deactivate entries by id (kept in the index as history, out of all
    retrieval). Returns the forgotten contents."""
    forgotten = []
    with locked(_index_path()):
        entries = _load()
        for entry in entries:
            if entry["id"] in entry_ids and entry["active"]:
                entry["active"] = False
                forgotten.append(entry["content"])
                log_event("memory_forgotten", content=entry["content"][:80])
        _save(entries)
    return forgotten
