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
                "emotional", "sensitive",
                "disputed"}  # P3.5d: cross-person contradiction, unresolved
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


def _mirror(changed: list, all_entries: list | None = None) -> None:
    """P3.2a: mirror changed entries into Postgres AFTER the file write.
    Files are the authority — a PG failure logs fact_sync_deferred inside
    mirror_entries and never raises into the caller.

    `all_entries` is the caller's already-loaded full index, used to
    REPAIR a stale mirror. It is passed in rather than re-read because
    every caller holds the index lock — re-reading here deadlocked the
    whole suite."""
    try:
        from kyraan.store import facts
        facts.mirror_entries(changed, all_entries=all_entries)
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
    from kyraan.control_plane import kernel as _kernel
    # Fail-closed authorship (2026-08-28 sweep): an unidentified viewer
    # is "unknown", never the owner — visibility clauses treat unknown
    # authors as strangers, so a mis-set turn can't mint owner facts.
    author = _kernel.effective_reviewer() or "unknown"
    changed, disputes = [], []
    if supersedes:
        old_words = _words(supersedes)
        for entry in entries:
            if not entry["active"]:
                continue
            entry_words = _words(entry["content"])
            if entry_words and (entry_words <= old_words
                                or (len(old_words) >= 3 and old_words <= entry_words)):
                if entry.get("author", "owner") != author:
                    # P3.5d (arch §4): a contradiction ACROSS people never
                    # supersedes — supersession stays within one
                    # reviewer's authority. Both facts stand, flagged,
                    # and the subject-owner's queue gets the dispute.
                    entry["flags"] = sorted(set(entry.get("flags") or [])
                                            | {"disputed"})
                    flags = sorted(set(flags) | {"disputed"})
                    disputes.append(entry)
                    changed.append(entry)
                    log_event("memory_disputed", old=entry["content"][:80],
                              new=content[:80], old_author=entry.get("author", "owner"),
                              new_author=author)
                    continue
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
        "author": author, "active": True, "superseded_by": None,
    })
    _save(entries)
    # The new fact FIRST: superseded links point at it, and sync_entries'
    # two-pass order needs its row in the same batch.
    _mirror([entries[-1]] + changed, all_entries=entries)
    for old_entry in disputes:
        _file_dispute_notice(old_entry, entries[-1])
    _extract_triples_async(new_id, entries[-1]["content"])
    return new_id


def _subject_owner_for(target: str) -> str:
    """Whose review queue owns a dispute about this target: people/<n>
    when that person is enrolled at a stage that can actually REVIEW
    (a notice filed to someone who can't log in is a black hole — the
    owner holds it until they can), else the owner. PG trouble → owner."""
    name = target.split("/", 1)[1].removesuffix(".md") if target.startswith("people/") else ""
    if not name or name == "owner":
        return "owner"
    try:
        from kyraan.store import pg
        with pg.connection() as conn:
            row = conn.execute("SELECT stage FROM person WHERE id = %s",
                               (name,)).fetchone()
        return name if row and row[0] in ("read_mostly", "full") else "owner"
    except Exception:
        return "owner"


def flag_disputed(a_id: str, b_id: str) -> bool:
    """Mark two facts disputed (both stand; the flags are the visible
    state until the subject-owner resolves). Idempotent."""
    changed = []
    with locked(_index_path()):
        entries = _load()
        for entry in entries:
            if entry["id"] in (a_id, b_id) and entry["active"]:
                if "disputed" not in (entry.get("flags") or []):
                    entry["flags"] = sorted(set(entry.get("flags") or [])
                                            | {"disputed"})
                    changed.append(entry)
        if changed:
            _save(entries)
            _mirror(changed, all_entries=entries)
    return bool(changed)


def _file_dispute_notice(old_entry: dict, new_entry: dict) -> None:
    """P3.5d: a cross-person contradiction lands in the SUBJECT-OWNER's
    review queue as a resolvable notice — approve keeps the new claim
    (superseding the old under the reviewer's own authority), reject
    forgets the new one. Best-effort: a failure leaves both facts
    standing flagged, which is already the honest state."""
    try:
        from kyraan.memory import store as memory_store
        memory_store.file_dispute(
            target=new_entry["target"],
            reviewer=_subject_owner_for(new_entry["target"]),
            old_id=old_entry["id"], new_id=new_entry["id"],
            old_content=old_entry["content"], new_content=new_entry["content"])
    except Exception as exc:
        log_event("dispute_notice_failed", reason=str(exc)[:150])


def clear_flag(entry_ids: list, flag: str) -> None:
    with locked(_index_path()):
        entries = _load()
        changed = []
        for entry in entries:
            if entry["id"] in entry_ids and flag in (entry.get("flags") or []):
                entry["flags"] = [f for f in entry["flags"] if f != flag]
                changed.append(entry)
        if changed:
            _save(entries)
            _mirror(changed, all_entries=entries)


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


def all_entries() -> list:
    """EVERY index entry, active or not — what a full PG resync needs:
    deactivations (forget/supersede) only propagate if the inactive rows
    travel too."""
    with locked(_index_path()):
        return _load()


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
            _mirror(expired, all_entries=entries)
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
    # RAG arm (2026-08-27): semantic neighbours of the message join the
    # candidate pool — "what do I do for stress" reaches "meditates
    # daily" with zero word overlap. Embedder down ⇒ the arm just
    # doesn't fire; FTS and flags carry on.
    qvec = None
    if message.strip():
        try:
            import json as _json

            from kyraan.store import embed as _embed
            qvec = _json.dumps(_embed.embed([message])[0])
        except Exception:
            qvec = None
    from kyraan.store import sync_state as _sync_state
    if _sync_state.is_stale("facts"):
        # A known-behind mirror must not answer: a forgotten fact whose
        # deactivation never landed would resurface (Bugbot P1).
        log_event("memory_backend_fallback", backend="pg", reason="mirror stale")
        return None
    # P3.5c: the §4 visibility clause. The owner sees everything except
    # subject_only facts of OTHER people (those route to their own
    # review); any other viewer sees shared facts plus facts about
    # themselves — nothing else, whatever the retrieval heuristics say.
    from kyraan.control_plane import kernel as _kernel
    viewer = _kernel.viewer_person()
    if viewer == "owner":
        vis_sql = "AND NOT (visibility = 'subject_only' AND subject <> 'owner')"
        vis_params: tuple = ()
    else:
        vis_sql = "AND (visibility = 'shared' OR subject = %s)"
        vis_params = (viewer,)
    try:
        with _pg.connection() as conn:
            rows = conn.execute(
                f"""SELECT legacy_id, content, target, kind, term, importance,
                          flags, era, sphere, created_at,
                          CASE WHEN %s::vector IS NOT NULL
                                    AND embedding IS NOT NULL
                               THEN 1 - (embedding <=> %s::vector) END AS sim
                   FROM fact
                   WHERE active AND owner = 'owner'
                         {vis_sql}
                         AND NOT (term = 'short' AND created_at < %s)
                         AND (flags && ARRAY['health','safety','emergency','danger']
                              OR importance = 'critical' OR kind = 'identity'
                              OR (%s <> '' AND to_tsvector('english', content)
                                              @@ to_tsquery('english', %s))
                              OR (%s::vector IS NOT NULL
                                  AND id IN (SELECT id FROM fact
                                             WHERE active AND embedding IS NOT NULL
                                             ORDER BY embedding <=> %s::vector
                                             LIMIT 12))
                              OR id IN (SELECT id FROM fact WHERE active
                                        ORDER BY created_at DESC LIMIT 100))
                   ORDER BY created_at, legacy_id""",
                (qvec, qvec, *vis_params, cutoff, tsquery, tsquery or "x",
                 qvec, qvec)).fetchall()
    except Exception as exc:
        log_event("memory_backend_fallback", backend="pg",
                  reason=str(exc)[:200])
        return None
    return [{"id": r[0], "content": r[1], "target": r[2], "kind": r[3],
             "term": r[4], "importance": r[5], "flags": list(r[6] or []),
             "era": r[7], "sphere": r[8], "created": r[9].isoformat(),
             "active": True, "superseded_by": None,
             "_sim": float(r[10]) if r[10] is not None else None}
            for r in rows]


def build_context(message: str = "", budget_chars: int = 3500) -> str:
    """The memory block for a prompt: safety-critical and identity facts
    ALWAYS included; the rest ranked by relevance to the current message,
    then importance, then recency — filled to budget, never blind-cut.
    KYRAAN_MEMORY_BACKEND=pg swaps candidate RETRIEVAL to Postgres
    (P3.2b); the ranking/discretion code below is shared verbatim."""
    import os as _os
    entries = None
    # P3.2c cutover (2026-08-27, owner waived the remaining soak days):
    # pg is the DEFAULT read path; =files is the rollback lever. Writes
    # stay dual and files remain the authority indefinitely.
    if _os.environ.get("KYRAAN_MEMORY_BACKEND", "pg").strip().lower() == "pg":
        entries = _pg_candidates(message)
    if entries is None:
        from kyraan.control_plane import kernel as _kernel
        if _kernel.viewer_person() != "owner":
            # P3.5c fail-closed: the index file has no visibility
            # columns, so a non-owner NEVER falls back to it — a pg
            # outage means they get no facts, not the owner's.
            log_event("memory_visibility_failclosed",
                      viewer=_kernel.viewer_person())
            return ""
        entries = active_entries()
    if not entries:
        return ""
    message_words = _words(message)

    reaching_for_past = bool({w.lower() for w in message.split()} & _PAST_CUES)
    # RAG precision (2026-08-27): the single TOP semantic match, when it
    # clears a floor, counts as "strong, direct relevance" for the
    # discretion rule — "where do I stay?" ranked the (sensitive-flagged)
    # home fact #1 with a wide margin and word overlap still hid it. Only
    # rank #1 qualifies: a vague query's flat similarities can never
    # volunteer a sensitive memory.
    sims = [e.get("_sim") for e in entries if e.get("_sim") is not None]
    top_sim = max(sims) if sims else None
    always, ranked = [], []
    for entry in entries:
        safety = set(entry.get("flags") or []) & _SAFETY_FLAGS
        if safety or entry["importance"] == "critical" or entry["kind"] == "identity":
            always.append(entry)
            continue
        overlap = len(message_words & _words(entry["content"]))
        sim = entry.get("_sim")
        semantically_direct = (sim is not None and top_sim is not None
                               and sim >= 0.12 and sim >= top_sim)
        if (set(entry.get("flags") or []) & _DISCRETION_FLAGS
                and overlap < 2 and not semantically_direct):
            # A sensitive or emotional memory with no strong tie to the
            # current message stays private — discretion means absence,
            # not a lower rank.
            continue
        score = overlap * 10 + (5 if entry["importance"] == "high" else 0)
        # Similarity is a RANKING signal, not just a nomination — without
        # this a zero-overlap semantic hit scored 0 and survived only
        # while every fact fit the budget. Fact sims run LOW on short
        # texts (real matches ~0.15, junk ~0.03 — measured), so the
        # floor is 0.12 and the weight keeps a strong match under a
        # two-word direct overlap.
        if sim is not None and sim > 0.12:
            score += sim * 25
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

    def _render(entry) -> str:
        line = f"- {entry['content']}"
        if entry["flags"]:
            line += f"  [{'/'.join(entry['flags']).upper()}]"
        return line

    def fits(entry, force: bool = False) -> bool:
        nonlocal used
        line = _render(entry)
        if not force and used + len(line) > budget_chars:
            return False
        lines.append(line)
        used += len(line) + 1
        return True

    # "ALWAYS included" has to mean it. These are the health/safety/
    # emergency/danger, critical and identity facts — the ones whose
    # silent omission is the most dangerous thing this function can do —
    # and the budget was allowed to drop them (Bugbot P2). They are
    # written first and unconditionally; the budget then governs only the
    # ranked remainder, which is what a budget is FOR.
    for entry in always:
        fits(entry, force=True)
    if used > budget_chars:
        log_event("memory_budget_exceeded_by_always", chars=used,
                  budget=budget_chars, always=len(always))
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
            _mirror(changed, all_entries=entries)
            _sweep_episodes(changed)
    if forgotten:
        try:  # P3.5e: forgetting a recently auto-approved fact = a wrong
            from kyraan.memory import review_scaling  # auto-approval
            review_scaling.on_forgotten(forgotten)
        except Exception as exc:
            log_event("review_scaling_check_failed", reason=str(exc)[:120])
        _purge_matching_pending(forgotten)
    return forgotten


def _purge_matching_pending(forgotten_contents: list) -> None:
    """P3.7a resurrection channel closed: a PENDING proposal restating a
    forgotten fact re-enters local prompts through the pending block —
    found live when an eval fact 'resurrected' from the review queue.
    Forgetting a fact drops queued proposals that state it (word
    containment / 2+ overlap, the find_matches rule). Dispute notices
    are resolutions, not restatements — untouched."""
    try:
        forgotten_words = [w for w in (_words(c) for c in forgotten_contents) if w]
        for path in store.PENDING_DIR.glob("*.md"):
            text = path.read_text()
            if "\ndispute:" in text:
                continue
            _, _, rest = text.partition("---\n")
            _, _, body = rest.partition("---\n")
            body_words = _words(body.strip().lstrip("- ").strip())
            if not body_words:
                continue
            for words in forgotten_words:
                smaller = min(len(body_words), len(words))
                if ((smaller >= 3 and (body_words <= words or words <= body_words))
                        or len(body_words & words) >= max(3, smaller // 2)):
                    path.unlink(missing_ok=True)
                    log_event("memory_pending_purged_by_forget",
                              proposal=path.name)
                    break
    except Exception as exc:
        log_event("pending_purge_failed", reason=str(exc)[:120])


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
            _mirror(changed, all_entries=entries)
    return superseded


def resweep_forgotten() -> int:
    """P3.3d self-heal (nightly + resync): re-run the episode sweep for
    every FORGOTTEN fact — inactive, no supersessor (an update is not a
    forget), long-term (an expired short is not a forget either).
    Idempotent; catches sweeps deferred by a PG outage."""
    from kyraan.store import documents, episodes, facts
    swept = 0
    for entry in _load():
        if (not entry.get("active") and not entry.get("superseded_by")
                and entry.get("term") != "short"):
            fact_id = facts.fact_uuid(entry["id"])
            swept += episodes.suppress_for_fact(fact_id, entry["content"])
            swept += documents.suppress_for_fact(fact_id, entry["content"])
    return swept


def unforget(entry_ids: list) -> list:
    """The forget inverse (undo matrix completion, 2026-08-28):
    reactivate entries and lift their suppression marks from episodes
    and documents. Pending proposals purged at forget time stay gone —
    an undo restores the fact, not the queue's history."""
    restored, changed = [], []
    with locked(_index_path()):
        entries = _load()
        for entry in entries:
            if entry["id"] in entry_ids and not entry["active"]:
                entry["active"] = True
                restored.append(entry["content"])
                changed.append(entry)
        _save(entries)
        if changed:
            _mirror(changed, all_entries=entries)
            _unsweep_episodes(changed)
    for content in restored:
        log_event("memory_unforgotten", content=content[:80])
    return restored


def _unsweep_episodes(changed: list) -> None:
    try:
        from kyraan.store import facts, pg
        if not facts.MIRROR_ENABLED:
            return
        with pg.connection() as conn:
            for entry in changed:
                fact_id = facts.fact_uuid(entry["id"])
                for table in ("episode", "document"):
                    conn.execute(
                        f"""UPDATE {table} SET suppressed_by =
                                array_remove(suppressed_by, %s)
                            WHERE %s = ANY(suppressed_by)""",
                        (fact_id, fact_id))
            conn.commit()
    except Exception as exc:
        log_event("episode_suppress_deferred", reason=str(exc)[:200])


def _sweep_episodes(changed: list) -> None:
    """P3.3d: forget cascades to episodes — a forgotten fact must never
    resurface through recall (audit P1). Deferred failures are re-swept
    by scripts/resync_facts.py, which sweeps for every inactive fact."""
    try:
        from kyraan.store import episodes, facts
        if not facts.MIRROR_ENABLED:  # tests: no PG side-effects at all
            return
        from kyraan.store import documents
        total = docs = 0
        for entry in changed:
            fact_id = facts.fact_uuid(entry["id"])
            total += episodes.suppress_for_fact(fact_id, entry["content"])
            docs += documents.suppress_for_fact(fact_id, entry["content"])
        log_event("episodes_suppressed", facts=len(changed),
                  episodes=total, documents=docs)
    except Exception as exc:
        log_event("episode_suppress_deferred", reason=str(exc)[:200])


def memory_context(message: str = "") -> str:
    """THE memory block for any prompt, both brains (review P1: two call
    sites re-implemented this and one kept resurrecting forgotten facts).
    Once an index exists it is the sole authority; the Markdown dump
    serves only installs that never migrated."""
    if _index_path().exists():
        return build_context(message) or "(no facts stored yet)"
    from kyraan.control_plane import kernel as _kernel
    if _kernel.viewer_person() != "owner":
        return "(no facts stored yet)"  # P3.5c: the raw dump is owner-only
    return store.load_all_facts() or "(no facts stored yet)"


# ---------------------------------------------------------------------
# Similarity against the ACTIVE store (2026-09-04, owner: "do we check
# similarly? do we precisely extract?"). Word overlap catches restated
# facts with the same words; embeddings catch the same fact in other
# words. An audit of 39 active facts found one contradiction living
# beside its predecessor at cosine 0.76 — "every 5 minutes" vs "every
# hour" — that word rules never joined.
SIM_REPLACE = 0.85     # the same fact, reworded: the new one supersedes
SIM_SIMILAR = 0.72     # worth showing the owner side by side


def _active_with_vectors(subject: str = "") -> list:
    """[(id, subject, content, vector)] for active facts that carry an
    embedding, from the PG mirror."""
    try:
        import json as _j
        from kyraan.store import pg as _pg
        with _pg.connection() as conn:
            rows = conn.execute(
                """SELECT legacy_id, subject, content, embedding::text FROM fact
                   WHERE active AND embedding IS NOT NULL""").fetchall()
        return [(r[0], r[1], r[2], _j.loads(r[3])) for r in rows if r[3]]
    except Exception:
        return []


def similar_active(content: str, subject: str = "", limit: int = 2) -> list:
    """[(similarity, id, content)] best first, above SIM_SIMILAR."""
    try:
        from kyraan.store import embed as _embed
        import math
        vec = _embed.embed([content])[0]
    except Exception:
        return []
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    out = []
    for fid, subj, text, other in _active_with_vectors(subject):
        if subject and subj and subj != subject:
            continue
        on = math.sqrt(sum(x * x for x in other)) or 1.0
        sim = sum(a * b for a, b in zip(vec, other)) / (norm * on)
        if sim >= SIM_SIMILAR:
            out.append((round(sim, 3), fid, text))
    out.sort(reverse=True)
    return out[:limit]


def similarity_verdict(best: float) -> str:
    return "replace" if best >= SIM_REPLACE else "similar" if best >= SIM_SIMILAR else ""
