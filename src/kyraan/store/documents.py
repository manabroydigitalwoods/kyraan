"""Document memory (owner directive 2026-08-27): text captured from
photos (visiting cards, brochures) and PDFs, transcribed at ingestion,
chunked, embedded locally, and retrievable forever.

Retrieval is HYBRID on purpose: embeddings are bad at digits, so a
phone number is found by the FTS arm ("98300" matches exactly) while
"that AC repair guy from the brochure" is found by the semantic arm.

Privacy plumbing mirrors episodes: a sensitivity pass tags each
document; `exposure` gates which prompts a chunk may enter (local_only
chunks NEVER ride into a cloud-tier prompt); `suppressed_by` lets the
forget cascade cover documents; retrieval is chat-scoped.
"""
import hashlib
import json
import re
import uuid
from pathlib import Path

from kyraan.control_plane.logging_setup import log_event
from kyraan.store import embed, pg

DOC_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "documents.kyraan.local")
CHUNK_CHARS = 800
SEARCH_MIN_SIM = 0.28  # calibrated 2026-08-27 on the probe card: true
                       # paraphrases ("who fixes water pipes",
                       # "plumber's visiting card") measure 0.34-0.35,
                       # the noise ceiling 0.19 — documents sit in a
                       # lower sim regime than episodes (0.35 there)
_MIN_TEXT_CHARS = 20


def _doc_uuid(chat_id: int, text: str) -> str:
    digest = hashlib.sha1(text.strip().encode()).hexdigest()[:20]
    return str(uuid.uuid5(DOC_NS, f"{chat_id}:{digest}"))


def _chunks(text: str) -> list:
    """Paragraph-respecting chunks ≤ CHUNK_CHARS. A visiting card is one
    chunk; a 20-page PDF becomes dozens, each separately findable."""
    pieces, current = [], ""
    for para in text.split("\n\n"):
        candidate = (current + "\n\n" + para).strip() if current else para.strip()
        if len(candidate) <= CHUNK_CHARS:
            current = candidate
            continue
        if current:
            pieces.append(current)
        while len(para) > CHUNK_CHARS:  # a single huge paragraph
            pieces.append(para[:CHUNK_CHARS])
            para = para[CHUNK_CHARS:]
        current = para.strip()
    if current:
        pieces.append(current)
    return pieces or [text.strip()]


def _name_map() -> dict:
    """Every known name/alias -> person id (the persons resolver)."""
    try:
        from kyraan.store import persons
        return persons.name_map()
    except Exception:
        return {}


def _registry_ids() -> list:
    return sorted(set(_name_map().values()))


def valid_subjects(candidates) -> list:
    """Only persons the registry RESOLVES are ever stored as a document's
    subjects — the model may PROPOSE names, the resolver decides (house
    pattern; aliases count: "Titu" -> titu_roy). Owner is never a
    subject: his docs are his by default."""
    if isinstance(candidates, str):
        candidates = [candidates]
    mapping = _name_map()
    out = []
    for candidate in candidates or []:
        wanted = str(candidate or "").strip().lower()
        if wanted == "owner":
            # An EXPLICIT self-reference (caption "my supplement", "me
            # and Kiaan") links the owner — owner 2026-09-02: "I said
            # my supplement but no connection was built". Only the
            # literal id qualifies; a name resolving to the owner still
            # does not (his docs are his by default, unlinked).
            if "owner" not in out:
                out.append("owner")
            continue
        pid = mapping.get(wanted) or mapping.get(wanted.replace(" ", "_"))
        if pid and pid != "owner" and pid not in out:
            out.append(pid)
    return out


def subjects_from_name(title: str) -> list:
    """Deterministic subjects from a human title: every known person
    NAME OR ALIAS used possessively ("Kiaan's vaccination card", "Titu's
    invoice"), with "and", or as "for/about <name>". Plain mentions
    don't count — a shop called "Ruma Stores" must not tag the doc as
    being about Ruma."""
    import re
    low = str(title or "").lower()
    out = []
    for name, pid in _name_map().items():
        if pid == "owner" or pid in out:
            continue
        pattern = re.escape(name)
        if re.search(rf"\b{pattern}(?:[’']s\b|\s+and\b)"
                     rf"|\b(?:for|about) {pattern}\b", low):
            out.append(pid)
    return sorted(out)


def caption_people(caption: str) -> list:
    """Subjects from a PHOTO-MOMENT caption (2026-09-02, live: "me and
    kiaan" linked nobody by caption). Unlike document titles — where a
    plain mention must not tag ("Ruma Stores" is a shop) — a moment
    caption names who is IN the picture, so bare registry names and
    aliases count as whole words. Bounded by the registry: nothing
    outside name_map can match. The owner is intentionally absent,
    matching valid_subjects' rule — his moments are his by default."""
    import re
    low = str(caption or "").lower()
    out = []
    if re.search(r"\b(?:me|my|mine|myself)\b", low):
        out.append("owner")   # explicit self-reference -> the owner node
    for name, pid in _name_map().items():
        if pid == "owner" or pid in out:
            continue
        if re.search(rf"\b{re.escape(name)}\b", low):
            out.append(pid)
    return sorted(out)


def link_person_to_latest_moment(chat_id: int, person_id: str,
                                 max_age_min: int = 20):
    """The owner naming someone in the photo JUST sent ("that is ruma")
    must stick to the stored moment (live 2026-09-02: the correction
    was acknowledged and nothing changed). Returns (caption,
    prior_subjects) or None when no recent moment exists."""
    with pg.connection() as conn:
        row = conn.execute(
            """SELECT id, caption, subject_persons FROM document
               WHERE chat_id = %s AND kind = 'moment'
                     AND created_at > now() - make_interval(mins => %s)
               ORDER BY created_at DESC LIMIT 1""",
            (chat_id, max_age_min)).fetchone()
        if row is None:
            return None
        doc_id, caption, subjects = row
        subjects = list(subjects or [])
        if person_id not in subjects:
            conn.execute(
                "UPDATE document SET subject_persons = %s WHERE id = %s",
                (subjects + [person_id], doc_id))
            conn.commit()
    log_event("moment_person_linked", doc_id=str(doc_id),
              person=person_id)
    return caption, subjects


def latest_capture(chat_id: int, max_age_h: int = 24) -> dict | None:
    """The photo/file the owner sent most recently, with everything it
    connects to — the deterministic answer to "did you save it?" / "links?"
    (live 2026-09-03: asked 15 minutes after a photo, the loop replied
    "what do you mean by it?"; the capture is the referent, always)."""
    with pg.connection() as conn:
        row = conn.execute(
            """SELECT d.id, d.kind, d.caption, d.created_at, d.subject_persons,
                      d.entities,
                      (SELECT array_agg(r.caption) FROM document r
                        WHERE r.id = ANY(d.related) AND r.suppressed_by = '{}')
               FROM document d
               WHERE d.chat_id = %s AND d.suppressed_by = '{}'
                     AND d.kind IN ('photo', 'moment', 'pdf', 'file', 'docx', 'text')
                     AND d.created_at > now() - make_interval(hours => %s)
               ORDER BY d.created_at DESC LIMIT 1""",
            (chat_id, max_age_h)).fetchone()
    if row is None:
        return None
    doc_id, kind, caption, created, subjects, ents, related = row
    ents = list(ents or [])
    return {"doc_id": str(doc_id), "kind": kind, "caption": caption or "(untitled)",
            "created": created, "subjects": list(subjects or []),
            "entities": [e for e in ents if not e.startswith("#")],
            "tags": [e for e in ents if e.startswith("#")],
            "related": [r for r in (related or []) if r]}


def describe_capture(cap: dict) -> str:
    kind = {"moment": "photo memory", "photo": "document photo"}.get(cap["kind"], cap["kind"])
    when = cap["created"].astimezone().strftime("%d %b %Y %H:%M") if cap.get("created") else ""
    lines = [f'Yes — saved as "{cap["caption"]}" ({kind}, {when}).']
    if cap["subjects"]:
        lines.append("About: " + ", ".join(cap["subjects"]))
    if cap["related"]:
        lines.append("Linked to: " + "; ".join(f'"{r}"' for r in cap["related"]))
    if cap["entities"]:
        lines.append("Named things: " + ", ".join(cap["entities"]))
    if cap["tags"]:
        lines.append("Filed under: " + " ".join(cap["tags"]))
    return "\n".join(lines)


SIMILAR_MIN_SIM = 0.5   # same-subject Kiaan photos scored 0.60-0.63 live;
                        # an unrelated same-subject card 0.09 (2026-09-03)


def similar_captures(doc_id: str, k: int = 5, min_sim: float = SIMILAR_MIN_SIM) -> list:
    """Captures that look like this one: a SHARED registry person and a
    close description (owner 2026-09-03: "can you similar images for
    kiaan?"). Same chat only; best first."""
    with pg.connection() as conn:
        rows = conn.execute(
            """WITH me AS (
                   SELECT d.id, d.chat_id, d.subject_persons, c.embedding
                   FROM document d JOIN document_chunk c
                        ON c.document_id = d.id AND c.seq = 0
                   WHERE d.id = %s)
               SELECT d.id, d.caption, d.created_at::date, d.subject_persons,
                      1 - (c.embedding <=> me.embedding) AS sim
               FROM me, document d
               JOIN document_chunk c ON c.document_id = d.id AND c.seq = 0
               WHERE d.chat_id = me.chat_id AND d.id <> me.id
                     AND d.kind IN ('moment', 'photo') AND d.suppressed_by = '{}'
                     AND d.subject_persons && me.subject_persons
                     AND c.embedding IS NOT NULL AND me.embedding IS NOT NULL
               ORDER BY sim DESC LIMIT %s""", (doc_id, k)).fetchall()
    return [{"doc_id": str(i), "caption": cap or "(untitled)", "date": day.isoformat(),
             "subjects": list(subs or []), "sim": float(sim)}
            for i, cap, day, subs, sim in rows if sim is not None and sim >= min_sim]


def link_captures(doc_id: str, other_ids: list) -> int:
    """Owner-asked links between captures ("link it with them"):
    symmetric, in `related`, like a capture and its note."""
    if not other_ids:
        return 0
    with pg.connection() as conn:
        for oid in other_ids:
            for a, b in ((doc_id, oid), (oid, doc_id)):
                conn.execute(
                    """UPDATE document
                       SET related = (SELECT coalesce(array_agg(DISTINCT r), '{}')
                                      FROM unnest(related || %s::uuid[]) r)
                       WHERE id = %s""", ([str(b)], a))
        conn.commit()
    log_event("captures_linked", capture=str(doc_id), to=[str(o) for o in other_ids])
    return len(other_ids)


def claim_latest_moment(chat_id: int, phrase: str, max_age_min: int = 20):
    """The owner captioning the photo JUST sent as theirs ("this is my
    medicine", live 2026-09-03: acknowledged twice, nothing stored
    changed — the moment stayed "Moment — 03 Sep 2026", nobody's, no
    category). Deterministic: link the owner, name the moment by the
    phrase when it only had the default title, and give it a category
    from the words when it has none. Returns (caption, entities) or
    None when no recent moment exists."""
    from kyraan.store import entities as _ents
    phrase = " ".join(str(phrase or "").split())[:120]
    with pg.connection() as conn:
        row = conn.execute(
            """SELECT id, caption, subject_persons, entities, text
               FROM document
               WHERE chat_id = %s AND kind IN ('moment', 'photo')
                     AND created_at > now() - make_interval(mins => %s)
               ORDER BY created_at DESC LIMIT 1""",
            (chat_id, max_age_min)).fetchone()
        if row is None:
            return None
        doc_id, caption, subjects, ents, text = row
        subjects = list(subjects or [])
        ents = list(ents or [])
        if "owner" not in subjects:
            subjects.append("owner")
        default_title = bool(re.match(r"^Moment — \d", str(caption or "")))
        if phrase and (default_title or not caption):
            caption = phrase
        if not any(e.startswith("#") for e in ents):
            tag = _ents.category_from_words(f"{phrase} {text[:400]}")
            if tag:
                ents.append(tag)
        conn.execute(
            """UPDATE document SET subject_persons = %s, caption = %s,
                                   entities = %s WHERE id = %s""",
            (subjects, caption[:300], ents, doc_id))
        conn.commit()
    log_event("moment_claimed_by_owner", doc_id=str(doc_id),
              caption=caption[:80], entities=ents)
    return caption, ents


FILES_DIR = Path(__file__).resolve().parents[3] / "data" / "documents"


def _store_original(doc_id: str, original: tuple) -> str:
    """Persist the uploaded bytes under data/documents/<doc_id>.<ext>
    (owner-only perms); returns the RELATIVE path stored on the row.
    Idempotent like the doc id itself — re-sending overwrites in place."""
    import os
    data, ext = original
    ext = "." + str(ext).strip(". ").lower()
    if ext not in (".jpg", ".jpeg", ".png", ".pdf", ".txt", ".csv",
                   ".md", ".json", ".log", ".docx"):
        return ""
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        FILES_DIR.chmod(0o700)
    except OSError:
        pass
    path = FILES_DIR / f"{doc_id}{ext}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return f"data/documents/{doc_id}{ext}"


def original_file(chat_id: int, doc_id: str):
    """(absolute_path, suggested_filename) for a doc's stored original,
    or None — chat-scoped, and the path is rebuilt from OUR root plus
    the stored basename (never trusted as-is)."""
    with pg.connection() as conn:
        row = conn.execute(
            """SELECT file_path,
                      coalesce(nullif(caption, ''), filename, 'document')
               FROM document
               WHERE chat_id = %s AND id = %s AND suppressed_by = '{}'""",
            (chat_id, doc_id)).fetchone()
    if not row or not row[0]:
        return None
    basename = row[0].replace("\\", "/").split("/")[-1]
    path = FILES_DIR / basename
    if not path.exists():
        return None
    ext = path.suffix
    safe_name = "".join(c for c in row[1] if c.isalnum() or c in " .-_")[:60]
    return str(path), (safe_name or "document") + ext


def ingest(chat_id: int, kind: str, text: str, caption: str = "",
           filename: str = "", subjects=None,
           original: tuple | None = None, entities=None) -> str | None:
    """Store one captured document. Returns the doc id, or None when the
    text is too thin to be a document. Idempotent: the same text in the
    same chat is one document (re-sending a card doesn't duplicate).
    `subjects` are PROPOSED person ids (vision model / caller) — each
    sticks only if the person registry knows it; a possessive caption
    adds its people deterministically either way. One doc can be about
    several people (a family policy naming Ruma AND Kiaan)."""
    text = text.strip()
    if len(text) < _MIN_TEXT_CHARS:
        return None
    subjects = valid_subjects(subjects)
    subjects += [p for p in subjects_from_name(caption) if p not in subjects]
    file_hash = hashlib.sha256(original[0]).hexdigest() if original else ""
    if file_hash:
        # Byte-level dedup (owner, 2026-08-28): the same FILE re-sent
        # can OCR slightly differently and dodge the text-identity doc
        # id — identical bytes are the same document, full stop.
        with pg.connection() as conn:
            row = conn.execute(
                """SELECT id FROM document
                   WHERE chat_id = %s AND file_sha256 = %s""",
                (chat_id, file_hash)).fetchone()
        if row:
            log_event("document_deduped_by_hash", chat_id=chat_id,
                      doc_id=str(row[0]))
            return str(row[0])
    if not caption and not filename:
        # Last-resort human name: the first meaningful content line —
        # "show me all docs" listing photo "(untitled)" rows told the
        # owner nothing (2026-08-27). Callers with a better title
        # (owner's caption, the vision model's) pass it instead.
        first = next((ln.strip() for ln in text.splitlines()
                      if len(ln.strip()) >= 4), "")
        caption = first[:60]
    doc_id = _doc_uuid(chat_id, text)
    if not entities and kind in ("pdf", "text", "docx", "file"):
        # photos bring entities from the vision pass; file uploads get
        # the same hubs from a local-tier extraction (2026-09-02), so a
        # PDF invoice connects through its vendor and #invoice like a
        # photographed one. Contained: a failed extraction is just none.
        try:
            from kyraan.store import entities as _entities
            entities = _entities.extract(text, hint=caption)
        except Exception:
            entities = []
    from kyraan.store.episodes import sensitivity_flags
    flags = sensitivity_flags(text)
    try:
        vectors = embed.embed(_chunks(text))
    except Exception:
        vectors = [None] * len(_chunks(text))  # FTS still finds them
    with pg.connection() as conn:
        file_path = _store_original(doc_id, original) if original else ""
        conn.execute(
            """INSERT INTO document (id, chat_id, kind, caption, filename,
                                     text, flags, subject_persons,
                                     file_path, file_sha256, entities)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   caption = EXCLUDED.caption, flags = EXCLUDED.flags,
                   subject_persons = EXCLUDED.subject_persons,
                   entities = CASE WHEN cardinality(EXCLUDED.entities) > 0
                                   THEN EXCLUDED.entities
                                   ELSE document.entities END,
                   file_path = CASE WHEN EXCLUDED.file_path <> ''
                                    THEN EXCLUDED.file_path
                                    ELSE document.file_path END,
                   file_sha256 = CASE WHEN EXCLUDED.file_sha256 <> ''
                                      THEN EXCLUDED.file_sha256
                                      ELSE document.file_sha256 END""",
            (doc_id, chat_id, kind, caption[:300], filename[:200], text,
             flags, subjects, file_path, file_hash,
             [str(e).strip()[:60] for e in (entities or []) if str(e).strip()][:12]))
        conn.execute("DELETE FROM document_chunk WHERE document_id = %s",
                     (doc_id,))
        for seq, (chunk, vector) in enumerate(zip(_chunks(text), vectors)):
            conn.execute(
                """INSERT INTO document_chunk (id, document_id, seq, text,
                                               embedding)
                   VALUES (%s, %s, %s, %s, %s)""",
                (str(uuid.uuid4()), doc_id, seq, chunk,
                 json.dumps(vector) if vector else None))
        conn.commit()
    log_event("document_ingested", chat_id=chat_id, doc_kind=kind,
              chars=len(text), chunks=len(_chunks(text)), flags=flags)
    if kind in ("photo", "moment"):
        try:
            relate(doc_id)
        except Exception as exc:   # a link is a bonus; the capture is stored
            log_event("documents_relate_failed", error=str(exc)[:100])
    return doc_id


_STOP = frozenset(
    "the a an of in on at to for with and or my his her their our its this "
    "that today first 1st 2nd 3rd new day photo photos moment moments".split())


def _content_words(text: str) -> set:
    """Crude stems of the words that carry meaning ("dressed" ~ "dress",
    "standing" ~ "stand"); registry names are not content, the shared
    subject already carries that."""
    out = set()
    for w in re.findall(r"[a-z]+", str(text or "").lower()):
        if len(w) < 4 or w in _STOP or w in _name_map():
            continue
        for suf in ("ing", "ed", "es", "s"):
            if not w.endswith(suf) or len(w) - len(suf) < 4:
                continue
            if suf == "s" and w.endswith("ss"):          # dress, glass
                continue
            if suf == "es" and not w.endswith(("ses", "xes", "zes", "ches", "shes")):
                continue                                  # clothes -> clothe? no: keep
            w = w[:-len(suf)]
            break
        out.add(w)
    return out


def relate(doc_id: str) -> list:
    """Link a capture (photo/moment) to the notes it illustrates and a
    note to the captures that illustrate it — symmetric, deterministic
    (owner 2026-09-03: the milestone note "1st wear sree krishna dress"
    and the photo "today kiaan with lord shree krishna dressed" both
    linked Kiaan and never each other). Rule: same chat, a SHARED
    registry person, and the note's TITLE words recur in the capture —
    two of them, or all of them when the title is that short. A note
    with a body mentioning "standing" does not catch a photo of someone
    standing; the title is the claim. The capture also inherits the
    note's #tags, so #milestone joins it at the hub. Returns the ids
    newly related."""
    with pg.connection() as conn:
        me = conn.execute(
            """SELECT chat_id, kind, caption, text, subject_persons, entities,
                      related FROM document WHERE id = %s""",
            (doc_id,)).fetchone()
        if me is None:
            return []
        chat_id, kind, caption, text, subjects, ents, related = me
        subjects = list(subjects or [])
        if not subjects:
            return []
        i_am_note = kind == "note"
        others = conn.execute(
            """SELECT id, kind, caption, text, entities, related
               FROM document
               WHERE chat_id = %s AND suppressed_by = '{}' AND id <> %s
                     AND (kind = 'note') <> %s
                     AND subject_persons && %s::text[]""",
            (chat_id, doc_id, i_am_note, subjects)).fetchall()
        newly = []
        for oid, okind, ocap, otext, oents, orel in others:
            note_title = caption if i_am_note else ocap
            capture = f"{ocap} {otext}" if i_am_note else f"{caption} {text}"
            title_words = _content_words(note_title)
            if not title_words:
                continue
            hit = title_words & _content_words(capture)
            if len(hit) < 2 and hit != title_words:
                continue
            note_tags = [e for e in ((ents if i_am_note else oents) or [])
                         if str(e).startswith("#")]
            cap_id, cap_ents = ((oid, list(oents or [])) if i_am_note
                                else (doc_id, list(ents or [])))
            inherited = [t for t in note_tags if t not in cap_ents]
            conn.execute(
                """UPDATE document
                   SET related = (SELECT coalesce(array_agg(DISTINCT r), '{}')
                                  FROM unnest(related || %s::uuid[]) r)
                   WHERE id = %s""", ([str(oid)], doc_id))
            conn.execute(
                """UPDATE document
                   SET related = (SELECT coalesce(array_agg(DISTINCT r), '{}')
                                  FROM unnest(related || %s::uuid[]) r)
                   WHERE id = %s""", ([str(doc_id)], oid))
            if inherited:
                conn.execute(
                    "UPDATE document SET entities = entities || %s::text[] "
                    "WHERE id = %s", (inherited, cap_id))
            if str(oid) not in [str(r) for r in (related or [])]:
                newly.append(str(oid))
                log_event("documents_related", capture=str(cap_id),
                          note=str(doc_id if i_am_note else oid),
                          words=sorted(hit), inherited=inherited)
        conn.commit()
    return newly


def _allowed_exposures() -> tuple:
    """Which exposures may enter the CURRENT prompt: local_only chunks
    only when the tier answering right now resolves to a local endpoint."""
    try:
        from kyraan.agents import agent_loop
        from kyraan.model_router import router
        tier = agent_loop.current_tier()
        from kyraan.control_plane import config
        provider = (config.load().get("model_tiers", {}).get(tier) or {}).get("provider", "")
        if provider and router.provider_is_local(provider):
            return ("cloud_ok", "local_only")
    except Exception:
        pass
    return ("cloud_ok",)


def search(chat_id: int, query: str, k: int = 3, person: str = "") -> list:
    """Hybrid chunk retrieval: FTS (exact strings, DIGITS) + ANN
    (meaning), chat-scoped, suppression- and exposure-filtered. Returns
    [{'doc_id','kind','caption','date','text','sim','fts'}] best-first.
    `person` (2026-09-02, unified index) narrows to rows linked to that
    registry person — the precise-linking model made queryable."""
    import re as _re

    from kyraan.memory.engine import _words
    terms = [w for w in _words(query) if _re.fullmatch(r"[a-z0-9]+", w)]
    # digits ride as raw tokens too — "98300" must match even though
    # _words lowercases wording; numbers in the query are the whole point
    terms += [t for t in _re.findall(r"\d{4,}", query) if t not in terms]
    tsquery = " | ".join(terms)
    try:
        qvec = json.dumps(embed.embed([query])[0])
    except Exception:
        qvec = None
    with pg.connection() as conn:
        rows = conn.execute(
            """SELECT d.id, d.caption, d.filename, d.created_at::date, c.text,
                      CASE WHEN %s::vector IS NOT NULL AND c.embedding IS NOT NULL
                           THEN 1 - (c.embedding <=> %s::vector) END AS sim,
                      (%s <> '' AND to_tsvector('english', c.text)
                                    @@ to_tsquery('english', %s)) AS fts,
                      d.kind, d.subject_persons,
                      (SELECT array_agg(r.caption) FROM document r
                        WHERE r.id = ANY(d.related) AND r.suppressed_by = '{}')
               FROM document_chunk c JOIN document d ON d.id = c.document_id
               WHERE d.chat_id = %s AND d.suppressed_by = '{}'
                     AND d.exposure = ANY(%s)
                     AND (%s = '' OR %s = ANY(d.subject_persons))
               ORDER BY fts DESC, sim DESC NULLS LAST
               LIMIT 30""",
            (qvec, qvec, tsquery, tsquery or "x", chat_id,
             list(_allowed_exposures()), person or "", person or "")).fetchall()
    results = []
    for doc_id, caption, filename, day, text, sim, fts, kind, subj, rel in rows:
        if not fts and (sim is None or sim < SEARCH_MIN_SIM):
            continue  # neither arm actually matched
        results.append({"doc_id": str(doc_id), "kind": kind,
                        "caption": caption or filename or "(untitled)",
                        "date": day.isoformat(), "text": text,
                        "subjects": list(subj or []),
                        "related": [r for r in (rel or []) if r],
                        "sim": float(sim) if sim is not None else None,
                        "fts": bool(fts)})
        if len(results) >= k:
            break
    return results


def relevant_snippet(chat_id: int, message: str) -> str | None:
    """The auto-injection arm: the single best document chunk for this
    message, or None. FTS hits (exact strings/numbers) always qualify;
    semantic hits need the calibrated floor."""
    try:
        hits = search(chat_id, message, k=1)
    except Exception as exc:
        log_event("document_rag_skipped", reason=str(exc)[:120])
        return None
    if not hits:
        return None
    hit = hits[0]
    clipped = hit["text"][:400] + ("…" if len(hit["text"]) > 400 else "")
    return (f'[from a saved document "{hit["caption"]}", {hit["date"]}] '
            + clipped.replace("\n", " ⏎ "))


def full_text(chat_id: int, query: str, max_chars: int = 6000) -> dict | None:
    """The whole document (clipped), found by the same hybrid search —
    "summarize the PDF" needs more than one 400-char chunk (live
    2026-08-28: a summary ask dead-ended in scope interrogations because
    no tool could read the doc). Exposure-gated like every read."""
    hits = search(chat_id, query, k=1)
    if not hits:
        return None
    with pg.connection() as conn:
        row = conn.execute(
            """SELECT coalesce(nullif(caption, ''), filename, '(untitled)'),
                      created_at::date, text
               FROM document
               WHERE chat_id = %s AND id = %s AND suppressed_by = '{}'
                     AND exposure = ANY(%s)""",
            (chat_id, hits[0]["doc_id"],
             list(_allowed_exposures()))).fetchone()
    if not row:
        return None
    caption, day, text = row
    clipped = text[:max_chars] + ("…(clipped)" if len(text) > max_chars else "")
    return {"caption": caption, "date": day.isoformat(), "text": clipped}


def list_documents(chat_id: int, limit: int = 15, person: str = "") -> list:
    person = (valid_subjects(person) or [""])[0]
    with pg.connection() as conn:
        rows = conn.execute(
            """SELECT d.id, d.kind, d.caption, d.filename, d.created_at::date,
                      length(d.text), d.subject_persons,
                      (SELECT array_agg(r.caption) FROM document r
                        WHERE r.id = ANY(d.related) AND r.suppressed_by = '{}')
               FROM document d WHERE d.chat_id = %s AND d.suppressed_by = '{}'
                     AND (%s = '' OR %s = ANY(d.subject_persons))
               ORDER BY d.created_at DESC LIMIT %s""",
            (chat_id, person, person, limit)).fetchall()
    return [{"id": str(i), "kind": k, "caption": c or f or "(untitled)",
             "date": d.isoformat(), "chars": n, "subjects": list(s or []),
             "related": [x for x in (rel or []) if x]}
            for i, k, c, f, d, n, s, rel in rows]


def sweep_orphaned_files(min_age_s: int = 3600) -> int:
    """Delete stored originals whose document row no longer exists —
    the terminal cleanup for unlink failures and crash-in-the-gap
    leftovers (Bugbot round-3 P2: a deleted sensitive document's bytes
    could otherwise linger indefinitely). Age-guarded: ingest writes
    the file BEFORE its row commits, so only files older than
    min_age_s are candidates; runs nightly."""
    import time
    if not FILES_DIR.exists():
        return 0
    cutoff = time.time() - min_age_s
    candidates = []
    for path in FILES_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                candidates.append(path)
        except OSError:
            continue
    if not candidates:
        return 0
    stems = sorted({p.stem for p in candidates})
    with pg.connection() as conn:
        # id::text = ANY(text[]) — the cast is on OUR side, explicit
        # (round-4 P2: uuid vs text[] comparison portability). file_path
        # rides along so the check is the EXACT basename the row points
        # at — a superseded extension ({id}.jpg after a {id}.png
        # re-ingest) is an orphan too, not protected by its stem.
        rows = conn.execute(
            "SELECT file_path FROM document WHERE id::text = ANY(%s)",
            (stems,)).fetchall()
    live_basenames = {r[0].replace("\\", "/").split("/")[-1]
                      for r in rows if r[0]}
    swept = 0
    for path in candidates:
        if path.name in live_basenames:
            continue
        try:
            path.unlink(missing_ok=True)
            swept += 1
        except OSError as exc:
            log_event("document_orphan_sweep_failed", file=path.name,
                      error=str(exc)[:120])
    if swept:
        log_event("document_orphans_swept", count=swept)
    return swept


def rename_document(chat_id: int, doc_id: str, caption: str) -> str | None:
    """Set a document's human name; returns the prior name (for undo)
    or None when the doc isn't this chat's. The owner naming a capture
    in conversation ("this is Kiaan's vaccination card") must stick —
    found live 2026-08-27: the association evaporated and the card
    stayed findable only by its generic title."""
    with pg.connection() as conn:
        row = conn.execute(
            """SELECT coalesce(nullif(caption, ''), filename, '(untitled)')
               FROM document WHERE chat_id = %s AND id = %s""",
            (chat_id, doc_id)).fetchone()
        if not row:
            return None
        # People named in the new title JOIN the doc's subjects (union,
        # never replace): renaming "Kiaan's vaccination card" to plain
        # "vaccination card" must not orphan the link, and adding
        # "…and Ruma's" must not drop Kiaan.
        subjects = subjects_from_name(caption)
        conn.execute(
            """UPDATE document SET caption = %s,
                   subject_persons = (SELECT coalesce(array_agg(DISTINCT p),
                                                      '{}')
                                      FROM unnest(subject_persons || %s) p)
               WHERE id = %s""",
            (caption[:300], subjects, doc_id))
    log_event("document_renamed", chat_id=chat_id, caption=caption[:80],
              subjects=subjects)
    return row[0]


def delete_documents(chat_id: int, doc_ids: list) -> list:
    """Hard-delete documents by id (captures are the owner's to destroy;
    chunks cascade). Returns the deleted captions."""
    captions, doomed_files = [], []
    with pg.connection() as conn:
        for doc_id in doc_ids:
            row = conn.execute(
                "DELETE FROM document WHERE chat_id = %s AND id = %s "
                "RETURNING coalesce(nullif(caption, ''), filename, '(untitled)'), "
                "file_path",
                (chat_id, doc_id)).fetchone()
            if row and row[1]:
                doomed_files.append(row[1].replace("\\", "/").split("/")[-1])
            if row is not None:
                captions.append(row[0])
        conn.commit()
    # Originals go ONLY after the commit (Bugbot P1, 2026-08-28):
    # unlinking mid-transaction meant a later failure rolled the rows
    # back while the bytes were already gone — a permanent loss the
    # database claimed never happened. A crash in the gap leaves an
    # orphaned file, which is recoverable noise, not data loss. Each
    # unlink is contained (round-2 P2): one bad file must not abort
    # the rest of the cleanup or fail a delete the database already
    # committed — an orphan is logged, never raised.
    for basename in doomed_files:
        try:
            (FILES_DIR / basename).unlink(missing_ok=True)
        except OSError as exc:
            log_event("document_file_orphaned", file=basename,
                      error=str(exc)[:120])
    for caption in captions:
        log_event("document_deleted", chat_id=chat_id, caption=caption[:80])
    return captions


def suppress_for_fact(fact_id: str, fact_content: str) -> int:
    """The forget cascade, extended to documents (same overlap>=2 rule
    as episodes): a forgotten fact's documents stop being served."""
    from kyraan.memory.engine import _words
    words = _words(fact_content)
    need = 2 if len(words) >= 2 else 1
    swept = 0
    with pg.connection() as conn:
        rows = conn.execute(
            "SELECT id, text, suppressed_by FROM document").fetchall()
        for doc_id, text, suppressed in rows:
            if fact_id in [str(u) for u in (suppressed or [])]:
                continue
            if len(words & _words(text)) >= need:
                conn.execute(
                    """UPDATE document
                       SET suppressed_by = suppressed_by || %s::uuid
                       WHERE id = %s""", (fact_id, doc_id))
                swept += 1
        conn.commit()
    return swept
