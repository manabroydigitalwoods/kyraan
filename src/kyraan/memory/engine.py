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
from kyraan.control_plane.filelock import atomic_write_text, locked

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


def _mirror(changed: list) -> None:
    """P3.2a: mirror changed entries into Postgres AFTER the file write.
    Files are the authority — a PG failure logs fact_sync_deferred inside
    mirror_entries and never raises into the caller."""
    try:
        from kyraan.store import facts
        facts.mirror_entries(changed)
    except Exception as exc:  # import/config trouble must not break memory
        log_event("fact_sync_deferred", entries=len(changed),
                  reason=str(exc)[:200])


def _load() -> list:
    try:
        return json.loads(_index_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(entries: list) -> None:
    _index_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(_index_path(), json.dumps(entries, indent=1, ensure_ascii=False))


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
    clean = content.lstrip("- ").strip()
    for entry in entries:
        if entry["active"] and entry["target"] == target and entry["content"] == clean:
            # Idempotency for promote retries (review P2): the index is
            # the authority — re-registering the same fact is a no-op.
            return entry["id"]
    changed = []
    if supersedes:
        old_words = _words(supersedes)
        for entry in entries:
            if not entry["active"]:
                continue
            entry_words = _words(entry["content"])
            if entry_words and (entry_words <= old_words or old_words <= entry_words):
                entry["active"] = False
                entry["superseded_by"] = new_id
                changed.append(entry)
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
    # The new fact FIRST: superseded links point at it, and sync_entries'
    # two-pass order needs its row in the same batch.
    _mirror([entries[-1]] + changed)
    _extract_triples_async(new_id, entries[-1]["content"])
    return new_id


def _extract_triples_async(fact_id: str, content: str) -> None:
    """P3.6a: graph extraction off the promote path — a review approval
    must not wait on a model call. A missed extraction self-heals: the
    resync script extracts for any active fact with no triple rows."""
    try:
        from kyraan.store import facts, triples
        if not (facts.MIRROR_ENABLED and triples.EXTRACT_ENABLED):
            return  # tests: no PG/model side-effects
        import threading

        def _run():
            try:
                from kyraan.store import triples
                triples.extract_and_store(fact_id, content)
            except Exception as exc:
                log_event("triple_extract_deferred", fact=fact_id,
                          reason=str(exc)[:150])

        threading.Thread(target=_run, daemon=True).start()
    except Exception as exc:
        log_event("triple_extract_deferred", fact=fact_id, reason=str(exc)[:150])


def active_entries() -> list:
    """Live entries, with short-term expiry applied lazily — the whole
    read-modify-write runs under the index lock (review P1: an unlocked
    expiry save could overwrite a concurrent promote or forget)."""
    with locked(_index_path()):
        entries = _load()
        now = datetime.now(timezone.utc)
        expired = []
        for entry in entries:
            if (entry["active"] and entry.get("term") == "short"):
                try:
                    created = datetime.fromisoformat(entry["created"])
                except ValueError:
                    continue
                if now - created > timedelta(days=_SHORT_TERM_DAYS):
                    entry["active"] = False
                    expired.append(entry)
                    log_event("memory_short_term_expired", content=entry["content"][:80])
        if expired:
            _save(entries)
            _mirror(expired)
    return [e for e in entries if e["active"]]


def _pg_candidates(message: str) -> list | None:
    """P3.2b: the candidate pool from Postgres — safety/critical/identity
    facts, FTS matches on the message, and the newest 100 (so zero-overlap
    facts can still fill spare budget exactly as file mode allows). Only
    RETRIEVAL changes; ranking below is the same code. Returns None on
    any failure so the caller falls back to files."""
    import re as _re
    from kyraan.store import pg as _pg
    terms = [w for w in _words(message) if _re.fullmatch(r"[a-z0-9]+", w)]
    tsquery = " | ".join(terms)
    cutoff = datetime.now(timezone.utc) - timedelta(days=_SHORT_TERM_DAYS)
    try:
        with _pg.connection() as conn:
            rows = conn.execute(
                """SELECT legacy_id, content, target, kind, term, importance,
                          flags, era, sphere, created_at
                   FROM fact
                   WHERE active AND owner = 'owner'
                         AND NOT (term = 'short' AND created_at < %s)
                         AND (flags && ARRAY['health','safety','emergency','danger']
                              OR importance = 'critical' OR kind = 'identity'
                              OR (%s <> '' AND to_tsvector('english', content)
                                              @@ to_tsquery('english', %s))
                              OR id IN (SELECT id FROM fact WHERE active
                                        ORDER BY created_at DESC LIMIT 100))
                   ORDER BY created_at, legacy_id""",
                (cutoff, tsquery, tsquery or "x")).fetchall()
    except Exception as exc:
        log_event("memory_backend_fallback", backend="pg",
                  reason=str(exc)[:200])
        return None
    return [{"id": r[0], "content": r[1], "target": r[2], "kind": r[3],
             "term": r[4], "importance": r[5], "flags": list(r[6] or []),
             "era": r[7], "sphere": r[8], "created": r[9].isoformat(),
             "active": True, "superseded_by": None} for r in rows]


def build_context(message: str = "", budget_chars: int = 3500) -> str:
    """The memory block for a prompt: safety-critical and identity facts
    ALWAYS included; the rest ranked by relevance to the current message,
    then importance, then recency — filled to budget, never blind-cut.
    KYRAAN_MEMORY_BACKEND=pg swaps candidate RETRIEVAL to Postgres
    (P3.2b); the ranking/discretion code below is shared verbatim."""
    import os as _os
    entries = None
    if _os.environ.get("KYRAAN_MEMORY_BACKEND", "files").strip().lower() == "pg":
        entries = _pg_candidates(message)
    if entries is None:
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
    # highest score first; newer entries break ties (two stable passes —
    # "created" is an ISO string and can't be negated in one key)
    ranked.sort(key=lambda pair: pair[1]["created"], reverse=True)
    ranked.sort(key=lambda pair: -pair[0])

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
    forgotten, changed = [], []
    with locked(_index_path()):
        entries = _load()
        for entry in entries:
            if entry["id"] in entry_ids and entry["active"]:
                entry["active"] = False
                forgotten.append(entry["content"])
                changed.append(entry)
                log_event("memory_forgotten", content=entry["content"][:80])
        _save(entries)
        if changed:
            _mirror(changed)
            _sweep_episodes(changed)
    return forgotten


def consolidate(keep_id: str, dup_ids: list) -> list:
    """Owner-approved semantic dedup: mark `dup_ids` superseded by
    `keep_id` — the same mechanism a correction uses, so mirrors and the
    graph's read-side cascade handle downstream. NOT a forget: no
    episode sweep (the topic itself remains live). Returns the
    superseded contents."""
    superseded, changed = [], []
    with locked(_index_path()):
        entries = _load()
        by_id = {e["id"]: e for e in entries}
        keep = by_id.get(keep_id)
        if keep is None or not keep["active"]:
            raise ValueError(f"keep fact {keep_id!r} is not an active fact")
        for dup_id in dup_ids:
            entry = by_id.get(dup_id)
            if entry is None or not entry["active"] or dup_id == keep_id:
                continue
            entry["active"] = False
            entry["superseded_by"] = keep_id
            superseded.append(entry["content"])
            changed.append(entry)
            log_event("memory_consolidated", kept=keep["content"][:80],
                      superseded=entry["content"][:80])
        if changed:
            _save(entries)
            _mirror(changed)
    return superseded


def resweep_forgotten() -> int:
    """P3.3d self-heal (nightly + resync): re-run the episode sweep for
    every FORGOTTEN fact — inactive, no supersessor (an update is not a
    forget), long-term (an expired short is not a forget either).
    Idempotent; catches sweeps deferred by a PG outage."""
    from kyraan.store import episodes, facts
    swept = 0
    for entry in _load():
        if (not entry.get("active") and not entry.get("superseded_by")
                and entry.get("term") != "short"):
            swept += episodes.suppress_for_fact(
                facts.fact_uuid(entry["id"]), entry["content"])
    return swept


def _sweep_episodes(changed: list) -> None:
    """P3.3d: forget cascades to episodes — a forgotten fact must never
    resurface through recall (audit P1). Deferred failures are re-swept
    by scripts/resync_facts.py, which sweeps for every inactive fact."""
    try:
        from kyraan.store import episodes, facts
        if not facts.MIRROR_ENABLED:  # tests: no PG side-effects at all
            return
        total = 0
        for entry in changed:
            total += episodes.suppress_for_fact(
                facts.fact_uuid(entry["id"]), entry["content"])
        log_event("episodes_suppressed", facts=len(changed), episodes=total)
    except Exception as exc:
        log_event("episode_suppress_deferred", reason=str(exc)[:200])


def memory_context(message: str = "") -> str:
    """THE memory block for any prompt, both brains (review P1: two call
    sites re-implemented this and one kept resurrecting forgotten facts).
    Once an index exists it is the sole authority; the Markdown dump
    serves only installs that never migrated."""
    if _index_path().exists():
        return build_context(message) or "(no facts stored yet)"
    return store.load_all_facts() or "(no facts stored yet)"
