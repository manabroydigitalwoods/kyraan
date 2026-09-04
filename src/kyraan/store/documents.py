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
                        WHERE r.id = ANY(d.related) AND r.suppressed_by = '{}'
                              AND r.exposure = ANY(%s)),
                      (SELECT array_agg(r.caption) FROM document r
                        WHERE r.id = ANY(d.related) AND r.suppressed_by = '{}'
                              AND r.exposure = 'local_only')
               FROM document d
               WHERE d.chat_id = %s AND d.suppressed_by = '{}'
                     AND d.exposure = ANY(%s)
                     AND d.kind IN ('photo', 'moment', 'pdf', 'file', 'docx', 'text')
                     AND d.created_at > now() - make_interval(hours => %s)
               ORDER BY d.created_at DESC LIMIT 1""",
            (list(_allowed_exposures()), chat_id, list(_allowed_exposures()), max_age_h)).fetchone()
    if row is None:
        return None
    doc_id, kind, caption, created, subjects, ents, related, local_related = row
    ents = list(ents or [])
    # `related` is what the current tier may see; `related_local` is what
    # only the owner's own screen may see (the rail shows it, the
    # history keeps a placeholder — see orchestrator's saved_q rail)
    return {"doc_id": str(doc_id), "kind": kind, "caption": caption or "(untitled)",
            "created": created, "subjects": list(subjects or []),
            "entities": [e for e in ents if not e.startswith("#")],
            "tags": [e for e in ents if e.startswith("#")],
            "related": [r for r in (related or []) if r],
            "related_local": [r for r in (local_related or []) if r]}


def describe_capture(cap: dict) -> str:
    kind = {"moment": "photo memory", "photo": "document photo"}.get(cap["kind"], cap["kind"])
    when = cap["created"].astimezone().strftime("%d %b %Y %H:%M") if cap.get("created") else ""
    lines = [f'Yes — saved as "{cap["caption"]}" ({kind}, {when}).']
    if cap["subjects"]:
        lines.append("About: " + ", ".join(cap["subjects"]))
    linked = list(cap["related"]) + list(cap.get("related_local") or [])
    if linked:
        lines.append("Linked to: " + "; ".join(f'"{r}"' for r in linked))
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
                     AND d.exposure = ANY(%s)
                     AND d.subject_persons && me.subject_persons
                     AND c.embedding IS NOT NULL AND me.embedding IS NOT NULL
               ORDER BY sim DESC LIMIT %s""",
            (doc_id, list(_allowed_exposures()), k)).fetchall()
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


_MEDS_WORDS = re.compile(
    r"\b(?:medicine|medication|lozenge|tablet|capsule|syrup|drops?|ointment|gel|"
    r"supplement|omega|vitamin|prescription|dose|\d+\s?(?:mg|ml))\b", re.IGNORECASE)


def medications_for(chat_id: int, person: str) -> list:
    """The saved medicines and supplements of ONE person (owner
    2026-09-03: "what are my medications?" listed Kiaan's drops three
    times and missed the owner's own lozenges). A capture counts when it
    is about that person and is filed #medical/#supplement with
    medicine words in it — a vaccination-day selfie is #medical but not
    a medicine. Exposure-gated like every read."""
    with pg.connection() as conn:
        rows = conn.execute(
            """SELECT caption, text, entities, created_at::date
               FROM document
               WHERE chat_id = %s AND suppressed_by = '{}'
                     AND exposure = ANY(%s)
                     AND %s = ANY(subject_persons)
                     AND (entities && ARRAY['#medical', '#supplement']::text[]
                          OR text ~* 'medicine|supplement|prescription|tablet|capsule')
               ORDER BY created_at DESC""",
            (chat_id, list(_allowed_exposures()), person)).fetchall()
    out = []
    for caption, text, ents, day in rows:
        body = re.sub(r"^\[photo, [^\]]*\]\s*", "", str(text or ""))
        if not (_MEDS_WORDS.search(body) or _MEDS_WORDS.search(caption or "")
                or "#supplement" in (ents or [])):
            continue
        first = next((ln.strip() for ln in body.splitlines() if len(ln.strip()) > 8), "")
        first = re.split(r"[—•]|\. ", first)[0].strip()[:110]
        out.append({"caption": caption or "(untitled)", "detail": first,
                    "date": day.isoformat(),
                    "kind": "supplement" if "#supplement" in (ents or []) else "medicine"})
    return out


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
           original: tuple | None = None, entities=None,
           uploaded_by: str = "") -> str | None:
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
            # the re-send may carry what the first send lacked: a title,
            # people, entities (review 2026-09-03) — union them in
            with pg.connection() as conn:
                conn.execute(
                    """UPDATE document
                       SET caption = CASE WHEN %s <> '' THEN %s ELSE caption END,
                           subject_persons = (SELECT coalesce(array_agg(DISTINCT p), '{}')
                                              FROM unnest(subject_persons || %s::text[]) p),
                           entities = CASE WHEN cardinality(%s::text[]) > 0
                                           THEN %s::text[] ELSE entities END
                       WHERE id = %s""",
                    (caption[:300], caption[:300], subjects,
                     list(entities or []), list(entities or []), row[0]))
            return str(row[0])
    explicit_caption = bool(caption)
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
    # Discretion classes keep a capture off the cloud tier (review
    # 2026-09-03: flags were stored and never read). Health alone does
    # not: the owner's medicine photos are asked about in normal turns.
    exposure = "local_only" if set(flags) & {"sensitive", "emotional"} else "cloud_ok"
    try:
        vectors = embed.embed(_chunks(text))
    except Exception:
        vectors = [None] * len(_chunks(text))  # FTS still finds them
    with pg.connection() as conn:
        file_path = _store_original(doc_id, original) if original else ""
        conn.execute(
            """INSERT INTO document (id, chat_id, kind, caption, filename,
                                     text, flags, subject_persons,
                                     file_path, file_sha256, entities, exposure,
                                     uploaded_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   uploaded_by = CASE WHEN document.uploaded_by = '' THEN EXCLUDED.uploaded_by
                                      ELSE document.uploaded_by END,
                   caption = CASE WHEN %s THEN EXCLUDED.caption
                                  ELSE document.caption END,
                   flags = EXCLUDED.flags,
                   exposure = CASE WHEN document.exposure = 'local_only'
                                   THEN 'local_only' ELSE EXCLUDED.exposure END,
                   subject_persons = (SELECT coalesce(array_agg(DISTINCT p), '{}')
                                      FROM unnest(document.subject_persons
                                                  || EXCLUDED.subject_persons) p),
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
             [str(e).strip()[:60] for e in (entities or []) if str(e).strip()][:12],
             exposure, str(uploaded_by or "")[:40], explicit_caption))
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
            """SELECT id, kind, caption, text, entities, related, exposure
               FROM document
               WHERE chat_id = %s AND suppressed_by = '{}' AND id <> %s
                     AND (kind = 'note') <> %s
                     AND subject_persons && %s::text[]""",
            (chat_id, doc_id, i_am_note, subjects)).fetchall()
        newly = []
        my_exposure = conn.execute("SELECT exposure FROM document WHERE id = %s",
                                   (doc_id,)).fetchone()[0]
        for oid, okind, ocap, otext, oents, orel, oexp in others:
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
            # a local-only note's tags stay with the note (audit
            # 2026-09-03: the hint that MADE it local-only would otherwise
            # ride onto a cloud-visible capture); the link itself is
            # read-gated by exposure wherever captions are rendered
            note_exposure = my_exposure if i_am_note else oexp
            inherited = ([t for t in note_tags if t not in cap_ents]
                         if note_exposure == "cloud_ok" else [])
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
                if not i_am_note:
                    ents.extend(inherited)   # the next matching note sees them
            if str(oid) not in [str(r) for r in (related or [])]:
                newly.append(str(oid))
                log_event("documents_related", capture=str(cap_id),
                          note=str(doc_id if i_am_note else oid),
                          words=sorted(hit), inherited=inherited)
        conn.commit()
    return newly


_RELATION_LABELS = ("father", "mother", "spouse", "husband", "wife", "nominee", "guardian",
                    "son", "daughter", "brother", "sister", "witness", "employer", "care of", "c/o",
                    "referred by", "introducer", "emergency contact", "next of kin")


def people_roles(text: str) -> dict:
    """WHO a document is about versus who it merely names (owner
    2026-09-04: the tax computation was linked to his father because
    "Father's Name Ganak Roy" matched the registry — a mention, not a
    subject). A registry name whose preceding label is a relation
    (father's name, nominee, spouse…) is a mention with that role; any
    other match is a subject. The owner counts as a subject here (the
    caller decides how to store him). Returns
    {"subjects": [pid…], "mentions": [(pid, role)…]}."""
    raw = str(text or "")
    low = re.sub(r"[^a-z0-9]+", " ", raw.lower())
    padded = " " + low + " "
    subjects, mentions, seen = [], [], set()
    for name, pid in sorted(_name_map().items(), key=lambda kv: -len(kv[0])):
        if pid in seen or len(name) < 3 or f" {name} " not in padded:
            continue
        roles = set()
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", low):
            before = low[max(0, m.start() - 40):m.start()]
            for label in _RELATION_LABELS:
                if label in before:
                    word = label.split()[0] if label != "care of" else "care of"
                    roles.add("spouse" if word in ("husband", "wife") else word)
        seen.add(pid)
        if roles and low.count(f" {name} ") <= 2:
            mentions.append((pid, sorted(roles)[0]))
        else:
            subjects.append(pid)
    return {"subjects": subjects, "mentions": mentions}


def people_in_text(text: str) -> list:
    """Registry people a document is ABOUT (never the owner: his files
    are his by default — see people_roles for who is merely named)."""
    return valid_subjects([p for p in people_roles(text)["subjects"] if p != "owner"])


def _shared_entities(mine: list, theirs: list) -> tuple:
    """(shared non-tag entities, shared #tags) — case-insensitive."""
    a = {str(e).strip().lower() for e in (mine or []) if str(e).strip()}
    b = {str(e).strip().lower() for e in (theirs or []) if str(e).strip()}
    both = a & b
    return (sorted(x for x in both if not x.startswith("#")),
            sorted(x for x in both if x.startswith("#")))


def links_for(entities_mine: list, entities_theirs: list) -> bool:
    """Two documents belong together when they share two named things,
    or one named thing under the same #category (the PAN on a challan
    and on the return; the same vendor on two #invoice PDFs)."""
    named, tags = _shared_entities(entities_mine, entities_theirs)
    if any(n.startswith("id:") for n in named):        # the same PAN, policy, PNR
        return True
    return len(named) >= 2 or (len(named) >= 1 and len(tags) >= 1)


def enrich(doc_id: str) -> dict:
    """The saver engine's second pass (owner 2026-09-04: "after read
    those doc kyraan should auto link if find something connectable"):
    people named in the text become subjects; documents sharing named
    entities become related, both ways; captures still get relate().
    Returns what was linked, for the receipt."""
    out = {"people": [], "related": [], "tags": [], "uploaded_by": "", "mentions": [], "about_owner": False}
    with pg.connection() as conn:
        me = conn.execute(
            """SELECT chat_id, kind, text, subject_persons, entities, related, uploaded_by
               FROM document WHERE id = %s""", (doc_id,)).fetchone()
        if me is None:
            return out
        chat_id, kind, text, subjects, ents, related, uploaded_by = me
        out["uploaded_by"] = uploaded_by or ""
        out["tags"] = [e for e in (ents or []) if str(e).startswith("#")]
        roles = people_roles(text)
        # the local model's reading, when it has one, overrides the label
        # rules for people and adds kind, ids, issuer, dates, a title
        reading = None
        if kind in ("pdf", "file", "text", "docx", "photo"):
            try:
                from kyraan.store import doc_understanding
                fn_cap = conn.execute("SELECT filename, caption FROM document WHERE id = %s", (doc_id,)).fetchone()
                reading = doc_understanding.understand(text, fn_cap[0] or "", fn_cap[1] or "")
            except Exception as exc:
                log_event("doc_understand_error", error=str(exc)[:100])
        if reading and (reading["subjects"] or reading["mentions"]):
            roles = {"subjects": reading["subjects"], "mentions": reading["mentions"]}
        if reading:
            extra = ([f"id:{i}" for i in reading["ids"]]
                     + ([reading["issuer"]] if reading["issuer"] else [])
                     + ([f"#{reading['kind']}"] if reading["kind"] and not any(str(e).startswith("#") for e in (ents or [])) else []))
            extra = [e for e in extra if e not in (ents or [])]
            if extra or reading["date"] or reading["title"]:
                conn.execute(
                    """UPDATE document SET entities = entities || %s::text[],
                           event_date = coalesce(event_date, %s),
                           caption = CASE WHEN caption = '' THEN %s ELSE caption END
                       WHERE id = %s""",
                    (extra, reading["date"], reading["title"][:300], doc_id))
                ents = list(ents or []) + extra
            out["reading"] = reading
        out["about_owner"] = "owner" in roles["subjects"]
        if out["about_owner"] and "about:owner" not in (ents or []):
            conn.execute("UPDATE document SET entities = entities || '{about:owner}'::text[] WHERE id = %s", (doc_id,))
            ents = list(ents or []) + ["about:owner"]
        people = [p for p in valid_subjects([s for s in roles["subjects"] if s != "owner"])
                  if p not in (subjects or [])]
        # a mention is never a subject: a father named on a return does
        # not own the return (owner 2026-09-04)
        mentioned = [p for p, _ in roles["mentions"]]
        keep = [p for p in (subjects or []) if p not in mentioned]
        mention_ents = [f"mentions:{p}:{role}" for p, role in roles["mentions"]
                        if f"mentions:{p}:{role}" not in (ents or [])]
        if people or mention_ents or keep != list(subjects or []):
            conn.execute(
                """UPDATE document SET subject_persons = %s::text[],
                                       entities = entities || %s::text[] WHERE id = %s""",
                (keep + [p for p in people if p not in keep], mention_ents, doc_id))
        out["people"] = keep + [p for p in people if p not in keep]
        out["mentions"] = roles["mentions"]
        named_mine = [e for e in (ents or []) if not str(e).startswith("#")]
        if named_mine:
            others = conn.execute(
                """SELECT id, coalesce(nullif(caption, ''), filename, '(untitled)'), entities
                   FROM document WHERE chat_id = %s AND id <> %s AND suppressed_by = '{}'
                         AND cardinality(entities) > 0 ORDER BY created_at DESC LIMIT 300""",
                (chat_id, doc_id)).fetchall()
            for oid, ocap, oents in others:
                if not links_for(ents, oents):
                    continue
                for a, b in ((doc_id, oid), (oid, doc_id)):
                    conn.execute(
                        """UPDATE document SET related = (SELECT coalesce(array_agg(DISTINCT r), '{}')
                           FROM unnest(related || %s::uuid[]) r) WHERE id = %s""", ([str(b)], a))
                if str(oid) not in [str(r) for r in (related or [])]:
                    out["related"].append(ocap)
        conn.commit()
    if out["related"] or people:
        log_event("document_enriched", doc=str(doc_id)[:8], people=people,
                  related=len(out["related"]))
    try:
        if kind in ("photo", "moment", "note"):
            relate(doc_id)
    except Exception:
        pass
    return out


def receipt_line(links: dict) -> str:
    """One line for the save receipt: who, whom, what, and its kin."""
    bits = []
    who = links.get("uploaded_by")
    if who:
        bits.append("from you" if who == "owner" else f"from {who.replace('_', ' ')}")
    people = [p for p in links.get("people") or [] if p != "owner"]
    if links.get("about_owner"):
        people = ["you"] + people
    if people:
        bits.append("about " + ", ".join(p if p == "you" else p.replace("_", " ").title() for p in people))
    if links.get("mentions"):
        bits.append("mentions " + ", ".join(f"{p.replace('_', ' ').title()} ({role})"
                                            for p, role in links["mentions"][:3]))
    if links.get("tags"):
        bits.append(" ".join(links["tags"][:3]))
    if links.get("related"):
        bits.append("related: " + "; ".join(str(c)[:40] for c in links["related"][:3]))
    reading = links.get("reading") or {}
    head = ""
    if reading.get("title") or reading.get("summary"):
        head = "\n📎 " + (reading.get("title") or "") + (
            (" — " + reading["summary"]) if reading.get("summary") else "")
        if reading.get("amounts"):
            head += " (" + "; ".join(reading["amounts"]) + ")"
    return head + (("\n🔗 " + " · ".join(bits)) if bits else "")


def repair_suppression() -> int:
    """Lift every fact-sweep mark from documents that were never
    sweepable, then re-sweep captures under the strict rule."""
    with pg.connection() as conn:
        n = conn.execute(
            "UPDATE document SET suppressed_by = '{}' WHERE suppressed_by <> '{}' AND NOT (kind = ANY(%s))",
            (list(SWEEPABLE_KINDS),)).rowcount
        conn.execute("UPDATE document SET suppressed_by = '{}' WHERE kind = ANY(%s)", (list(SWEEPABLE_KINDS),))
        conn.commit()
    try:
        from kyraan.memory import engine
        engine.resweep_forgotten()
    except Exception as exc:
        log_event("document_resweep_failed", error=str(exc)[:100])
    log_event("document_suppression_repaired", restored=n)
    return n


def _allowed_exposures() -> tuple:
    """Which exposures may enter the CURRENT prompt: local_only chunks
    only when the tier answering right now resolves to a local endpoint."""
    try:
        from kyraan.agents import agent_loop
        from kyraan.model_router import router
        if router.tier_may_see_private(agent_loop.current_tier()):
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
                        WHERE r.id = ANY(d.related) AND r.suppressed_by = '{}'
                              AND r.exposure = ANY(%s)),
                      d.entities
               FROM document_chunk c JOIN document d ON d.id = c.document_id
               WHERE d.chat_id = %s AND d.suppressed_by = '{}'
                     AND d.exposure = ANY(%s)
                     AND (%s = '' OR %s = ANY(d.subject_persons))
               ORDER BY fts DESC, sim DESC NULLS LAST
               LIMIT 30""",
            (qvec, qvec, tsquery, tsquery or "x", list(_allowed_exposures()),
             chat_id, list(_allowed_exposures()), person or "", person or "")).fetchall()
    results = []
    for doc_id, caption, filename, day, text, sim, fts, kind, subj, rel, ents in rows:
        if not fts and (sim is None or sim < SEARCH_MIN_SIM):
            continue  # neither arm actually matched
        results.append({"doc_id": str(doc_id), "kind": kind,
                        "caption": caption or filename or "(untitled)",
                        "date": day.isoformat(), "text": text,
                        "subjects": list(subj or []),
                        "related": [r for r in (rel or []) if r],
                        "tags": [e for e in (ents or []) if str(e).startswith("#")],
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


def _name_words(query: str) -> list:
    return [w for w in re.findall(r"[a-z0-9]{4,}", query.lower())
            if w not in {"explain", "summarize", "summarise", "about", "what", "does", "this", "that",
                         "with", "from", "have", "show", "read", "open", "tell", "document", "file",
                         "saved", "please", "detail", "details"}]


def by_name(chat_id: int, query: str, exposures=None) -> str | None:
    """A document id whose FILENAME or caption carries a word of the ask
    (live 2026-09-04: "explain the computation" found nothing though
    Computation.pdf was saved 45 s earlier — chunk search never saw
    the name). Newest first; exposure-gated unless told otherwise."""
    words = _name_words(query)
    if not words:
        return None
    with pg.connection() as conn:
        for w in words:
            row = conn.execute(
                """SELECT id FROM document
                   WHERE chat_id = %s AND suppressed_by = '{}' AND exposure = ANY(%s)
                         AND (filename ILIKE %s OR caption ILIKE %s)
                   ORDER BY created_at DESC LIMIT 1""",
                (chat_id, list(exposures or _allowed_exposures()), f"%{w}%", f"%{w}%")).fetchone()
            if row:
                return str(row[0])
    return None


def local_only_match(chat_id: int, query: str, max_chars: int = 6000) -> dict | None:
    """The private document the ask names — for the local tier ONLY (the
    caller must never put this in a cloud prompt). None when the ask
    names no local-only document."""
    doc_id = by_name(chat_id, query, exposures=("local_only",))
    if not doc_id:
        return None
    with pg.connection() as conn:
        row = conn.execute(
            """SELECT coalesce(nullif(caption, ''), filename, '(untitled)'), created_at::date, text
               FROM document WHERE id = %s""", (doc_id,)).fetchone()
    if not row:
        return None
    caption, day, text = row
    return {"caption": caption, "date": day.isoformat(),
            "text": text[:max_chars] + ("…(clipped)" if len(text) > max_chars else "")}


def exposure_of(doc_id) -> str:
    try:
        with pg.connection() as conn:
            row = conn.execute("SELECT exposure FROM document WHERE id = %s", (str(doc_id),)).fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def full_text(chat_id: int, query: str, max_chars: int = 6000) -> dict | None:
    """The whole document (clipped), found by the same hybrid search —
    "summarize the PDF" needs more than one 400-char chunk (live
    2026-08-28: a summary ask dead-ended in scope interrogations because
    no tool could read the doc). Exposure-gated like every read."""
    hits = search(chat_id, query, k=1)
    doc_id = hits[0]["doc_id"] if hits else by_name(chat_id, query)
    if not doc_id:
        return None
    with pg.connection() as conn:
        row = conn.execute(
            """SELECT coalesce(nullif(caption, ''), filename, '(untitled)'),
                      created_at::date, text
               FROM document
               WHERE chat_id = %s AND id = %s AND suppressed_by = '{}'
                     AND exposure = ANY(%s)""",
            (chat_id, doc_id,
             list(_allowed_exposures()))).fetchone()
    if not row:
        return None
    caption, day, text = row
    clipped = text[:max_chars] + ("…(clipped)" if len(text) > max_chars else "")
    return {"caption": caption, "date": day.isoformat(), "text": clipped}


def list_documents(chat_id: int, limit: int = 15, person: str = "",
                   tag: str = "", kind: str = "", since_days: int = 0) -> list:
    """Exposure-gated like search (audit 2026-09-03: it listed local-only
    note titles to the cloud tier). Filters (2026-09-03, retrieval
    gaps): a household member, a #tag hub ("what is filed under
    #medical"), a kind (photo/moment/pdf/note), a recency window."""
    # An unknown person filters to NOTHING, not to everything (found by
    # the 2026-09-03 gate test: an unregistered name dropped the filter)
    person = (valid_subjects(person) or [str(person or "").strip().lower()])[0]
    tag = str(tag or "").strip().lower()
    if tag and not tag.startswith("#"):
        tag = "#" + tag
    limit = max(1, min(int(limit or 15), 50))
    with pg.connection() as conn:
        rows = conn.execute(
            """SELECT d.id, d.kind, d.caption, d.filename, d.created_at::date,
                      length(d.text), d.subject_persons,
                      (SELECT array_agg(r.caption) FROM document r
                        WHERE r.id = ANY(d.related) AND r.suppressed_by = '{}'
                              AND r.exposure = ANY(%s)),
                      d.entities
               FROM document d WHERE d.chat_id = %s AND d.suppressed_by = '{}'
                     AND d.exposure = ANY(%s)
                     AND (%s = '' OR %s = ANY(d.subject_persons))
                     AND (%s = '' OR %s = ANY(d.entities))
                     AND (%s = '' OR d.kind = %s)
                     AND (%s = 0 OR d.created_at > now() - make_interval(days => %s))
               ORDER BY d.created_at DESC LIMIT %s""",
            (list(_allowed_exposures()), chat_id, list(_allowed_exposures()),
             person, person, tag, tag, kind or "", kind or "",
             int(since_days or 0), int(since_days or 0), limit)).fetchall()
    return [{"id": str(i), "kind": k, "caption": c or f or "(untitled)",
             "date": d.isoformat(), "chars": n, "subjects": list(s or []),
             "related": [x for x in (rel or []) if x],
             "tags": [e for e in (ents or []) if str(e).startswith("#")]}
            for i, k, c, f, d, n, s, rel, ents in rows]


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


SWEEPABLE_KINDS = ("photo", "moment")


def sweep_hit(fact_content: str, text: str) -> bool:
    """Does a forgotten fact hide this capture? Its DISTINCTIVE words must
    recur — all of them when the fact has three or fewer, else three and
    most of them. Live 2026-09-04: the old any-two-words rule let
    "Father's name is Biren Roy" and "User goes by the name Ruma" hide 11
    of 35 documents, three tax PDFs among them, through "name" + "roy"."""
    words = _content_words(fact_content) | set(re.findall(r"\b[a-z]{3,}\b", fact_content.lower())) & set(_name_map())
    if not words:
        return False
    hit = words & (_content_words(text) | set(re.findall(r"\b[a-z]{3,}\b", str(text or "").lower())))
    if len(words) <= 3:
        return hit == words
    return len(hit) >= 3 and len(hit) >= 0.6 * len(words)


def suppress_for_fact(fact_id: str, fact_content: str) -> int:
    """The forget cascade for CAPTURES (photos, moments — things Kyraan
    was told in passing). Files, PDFs and notes the owner deliberately
    saved are never swept by a fact: the owner deletes those by name."""
    swept = 0
    with pg.connection() as conn:
        rows = conn.execute(
            "SELECT id, text, suppressed_by FROM document WHERE kind = ANY(%s)",
            (list(SWEEPABLE_KINDS),)).fetchall()
        for doc_id, text, suppressed in rows:
            if fact_id in [str(u) for u in (suppressed or [])]:
                continue
            if sweep_hit(fact_content, text):
                conn.execute(
                    """UPDATE document
                       SET suppressed_by = suppressed_by || %s::uuid
                       WHERE id = %s""", (fact_id, doc_id))
                swept += 1
        conn.commit()
    return swept
