"""Obsidian vault indexing (owner directive 2026-09-02): every .md file
under KYRAAN_VAULT_ROOT joins document memory as kind='note', linked
PRECISELY — persons resolved through the registry (whole-word names,
aliases, and [[wikilinks]]), entities from wikilinks and #tags, an
event_date from frontmatter or the text, and heading-aware chunks so a
retrieved passage carries its own context. Read-only: Kyraan never
writes into the vault.

Sync is idempotent and change-aware: a file's sha256 decides whether it
is re-indexed; a changed note supersedes its earlier row (same
source_path); a deleted note is suppressed (the forget cascade's own
mechanism), never left behind as a ghost.
"""
import hashlib
import json
import os
import re
import uuid
from datetime import date
from pathlib import Path

from kyraan.control_plane.logging_setup import log_event
from kyraan.store import embed, pg

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_TAG = re.compile(r"(?<![\w/])#([A-Za-z][\w/-]{1,40})")
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_LONG_DATE = re.compile(
    r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(20\d{2})\b",
    re.I)
_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
CHUNK_CHARS = 900
# suppressed_by is uuid[] (it points at the suppressing fact/episode);
# notes suppress for reasons, so each reason is a stable sentinel uuid.
SUPERSEDED = str(uuid.uuid5(uuid.NAMESPACE_URL, "kyraan:note_superseded"))
DELETED = str(uuid.uuid5(uuid.NAMESPACE_URL, "kyraan:note_deleted"))
_SENSITIVE_HINTS = ("health", "medical", "salary", "password", "bank",
                    "diagnosis", "therapy")


def vault_root() -> Path | None:
    raw = os.environ.get("KYRAAN_VAULT_ROOT", "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    return root if root.is_dir() else None


def _folder_list(var: str) -> list:
    return [f.strip().strip("/") for f in
            os.environ.get(var, "").split(",") if f.strip()]


def indexed_folders() -> list:
    """KYRAAN_VAULT_FOLDERS — the ONLY subfolders that get indexed
    (owner 2026-09-02: the vault holds work contracts and maybe
    personal docs; governance §2 keeps company data out entirely).
    Empty means: index nothing until the owner names folders."""
    return _folder_list("KYRAAN_VAULT_FOLDERS")


def local_only_folders() -> list:
    """KYRAAN_VAULT_LOCAL_ONLY — indexed, but their chunks never enter
    a cloud prompt (the email-bodies boundary, for personal notes)."""
    return _folder_list("KYRAAN_VAULT_LOCAL_ONLY")


def _under(rel: str, folders: list) -> bool:
    return any(rel == f or rel.startswith(f + "/") for f in folders)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [x.strip().strip("\"'") for x in str(value).strip("[]").split(",")
            if x.strip()]


def parse_note(text: str, rel_path: str) -> dict:
    """Frontmatter (title/tags/date/aliases), body, wikilinks, tags."""
    meta: dict = {}
    body = text
    m = _FRONTMATTER.match(text)
    if m:
        body = text[m.end():]
        current = None
        for line in m.group(1).splitlines():
            item = re.match(r"^\s+-\s*(.*)$", line)
            if item and current:
                # Obsidian writes list properties as YAML block lists
                # (live 2026-09-02: a milestone's people: list was lost
                # and linked nobody)
                meta[current] = (meta.get(current) or []) + [
                    item.group(1).strip().strip("\"'[]")]
                continue
            if ":" in line and not line.startswith((" ", "\t")):
                k, v = line.split(":", 1)
                current = k.strip().lower()
                v = v.strip().strip("\"'")
                meta[current] = ([x.strip().strip("\"'") for x in
                                  v.strip("[]").split(",") if x.strip()]
                                 if v.startswith("[") else v)
    title = meta.get("title") or Path(rel_path).stem.replace("_", " ")
    # [[Title|alias]] and [[Title#heading]] both point at the note "Title"
    links = []
    for raw in _WIKILINK.findall(body):
        target = re.split(r"[|#]", str(raw), 1)[0].strip()
        if target and target not in links:
            links.append(target)
    tags = [t.lower() for t in _TAG.findall(body)]
    for raw_tag in _as_list(meta.get("tags")):
        raw_tag = raw_tag.strip().strip("#").lower()
        if raw_tag:
            tags.append(raw_tag)
    return {"title": title[:200], "body": body.strip(), "links": links,
            "tags": sorted(set(tags)), "meta": meta}


def event_date_of(parsed: dict, mtime: float) -> date | None:
    raw = str(parsed["meta"].get("date", "") or "")
    for candidate in (raw, parsed["body"][:2000]):
        m = _ISO_DATE.search(candidate)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        m = _LONG_DATE.search(candidate)
        if m:
            try:
                return date(int(m.group(3)), _MONTHS[m.group(2).lower()[:3]],
                            int(m.group(1)))
            except (ValueError, KeyError):
                pass
    return None


def chunk_note(body: str) -> list:
    """Heading-aware chunks: each chunk is prefixed with its heading
    path so a retrieved passage carries its context."""
    chunks, path, buf = [], [], ""

    def flush():
        nonlocal buf
        if buf.strip():
            prefix = " > ".join(path)
            chunks.append((f"[{prefix}] " if prefix else "") + buf.strip())
        buf = ""

    for line in body.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            flush()
            level = len(h.group(1))
            path = path[:level - 1] + [h.group(2).strip()]
            continue
        if len(buf) + len(line) + 1 > CHUNK_CHARS:
            flush()
        buf += line + "\n"
    flush()
    # Template skeletons are not content (owner 2026-09-03: "why is Kiaan
    # connected to Suman, Rakesh, a place note?" — every note was an
    # empty Person/Place template, and identical boilerplate chunks made
    # them near-twins in the mesh). A chunk whose lines are all empty
    # field labels ("- Birthday — ", "- ") or bare headings carries
    # nothing to embed; an all-skeleton note yields NO chunks, and the
    # indexer stores its title without an embedding.
    return [c for c in chunks if substantive(c)]


_LABEL_ONLY = re.compile(r"^\s*(?:[-*]\s*)?(?:[^—:\n]{0,60}[—:])?\s*$")


def substantive(chunk: str) -> bool:
    body = re.sub(r"^\[[^\]]*\]\s*", "", chunk)   # heading path prefix
    words = []
    for line in body.splitlines():
        if _LABEL_ONLY.match(line):
            continue
        words += re.findall(r"[A-Za-z\u0900-\u097F]{2,}", line)
    return len(words) >= 3


def link_people(parsed: dict, rel: str = "") -> list:
    """Persons the note is ABOUT — registry-bounded: whole-word names or
    aliases in the body, plus [[wikilinks]] that resolve. Nothing the
    registry doesn't know can ever tag a note."""
    try:
        from kyraan.store import persons
        nm = persons.name_map()
    except Exception:
        return []
    found = []
    low = parsed["body"].lower()
    for name, pid in nm.items():
        if pid in ("owner",) or pid in found:
            continue
        if len(name) >= 3 and re.search(rf"\b{re.escape(name)}\b", low):
            found.append(pid)
    candidates = list(parsed["links"]) + _as_list(parsed["meta"].get("people"))
    for cand in candidates:
        pid = _resolve_name(nm, cand)
        if pid and pid != "owner" and pid not in found:
            found.append(pid)
    for seg in Path(rel).parts[:-1]:
        # a folder named after a person (milestone/kiaan/…) IS a signal
        pid = _resolve_name(nm, seg)
        if pid and pid != "owner" and pid not in found:
            found.append(pid)
    return sorted(found)


def _resolve_name(nm: dict, raw: str) -> str | None:
    """Registry resolution for a name in any form: exact key, underscored,
    or — when unambiguous — its first word ("Kiaan Roy" -> kiaan when
    exactly one registry key is "kiaan"). Never guesses between two."""
    low = str(raw or "").strip().lower()
    if not low:
        return None
    low = low.rstrip(".!,").replace("-", " ")
    hit = nm.get(low) or nm.get(low.replace(" ", "_"))
    if hit:
        return hit
    first = low.split()[0] if " " in low else ""
    if first and len(first) >= 3:
        # every identity whose ANY name starts with that first word —
        # two Kiaans in the registry means no guess at all
        ids = {pid for key, pid in nm.items()
               if key.replace("_", " ").split()[0] == first}
        if len(ids) == 1:
            return ids.pop()
    return None


def _slug(name: str) -> str:
    # the same id rule persons.add uses in the loop — one identity model
    return re.sub(r"[^a-z0-9_]+", "_",
                  name.lower().replace(" ", "_").replace("-", "_")).strip("_")


def is_person_note(parsed: dict, rel: str) -> bool:
    """A note that DESCRIBES a person: frontmatter `type: person`, or any
    note living under a people/ folder (the owner's convention)."""
    if str(parsed["meta"].get("type", "")).strip().lower() == "person":
        return True
    parts = [p.lower() for p in Path(rel).parts[:-1]]
    return "people" in parts or "persons" in parts


def register_person_note(parsed: dict, rel: str) -> str | None:
    """A person-note REGISTERS its person (owner directive 2026-09-02:
    "link any note to any person precisely"): the title (or frontmatter
    name) becomes a registry person — a contact-person with no chat, no
    access, exactly what persons.add grants — and frontmatter aliases
    become registry aliases, so every other note, photo, and fact that
    names them links from then on. The owner authored the note under
    people/ deliberately; that is the consent persons.add would ask for.
    Returns the person id."""
    try:
        from kyraan.store import persons
    except Exception:
        return None
    name = str(parsed["meta"].get("name") or parsed["title"]).strip()
    if len(name) < 2:
        return None
    try:
        # RESOLVE FIRST (live 2026-09-02: "Kiaan Roy" and "Ruma Roy" notes
        # created kiaan_roy and ruma_roy beside the real kiaan and ruma).
        # An existing identity gains the note's full name as an alias;
        # only a genuinely unknown name creates a person.
        pid = _resolve_name(persons.name_map(), name)
        if pid == "owner":
            return None
        if not pid:
            pid = _slug(name)
            if not pid or pid == "owner":
                return None
            known = set(persons.name_map().values()) | {row[0] for row in persons.list_persons()}
            if pid not in known:
                # an EXISTING id must never be re-enrolled — enroll's upsert
                # would wipe its chat_id and stage (review 2026-09-03)
                persons.enroll(pid, None, "none", None)
                log_event("person_registered_from_note", person=pid, path=rel)
        for alias in [name] + _as_list(parsed["meta"].get("aliases")):
            if alias and _slug(alias) != pid and alias.lower() != pid:
                persons.add_alias(pid, alias)
        return pid
    except Exception as exc:
        log_event("person_register_failed", path=rel, error=str(exc)[:100])
        return None


def _note_uuid(chat_id: int, rel_path: str, sha: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"note:{chat_id}:{rel_path}:{sha}"))


def _exposure(parsed: dict, flags: list, rel: str = "") -> str:
    text = (parsed["title"] + " " + " ".join(parsed["tags"])).lower()
    if (flags or any(h in text for h in _SENSITIVE_HINTS)
            or _under(rel, local_only_folders())):
        return "local_only"
    return "cloud_ok"


def index_file(chat_id: int, root: Path, path: Path, force: bool = False) -> str:
    """Index one note. Returns 'unchanged' | 'indexed' | 'skipped'."""
    rel = str(path.relative_to(root))
    try:
        raw = path.read_bytes()
    except OSError:
        return "skipped"
    sha = hashlib.sha256(raw).hexdigest()
    with pg.connection() as conn:
        row = conn.execute(
            """SELECT id, file_sha256 FROM document
               WHERE chat_id = %s AND kind = 'note' AND source_path = %s
                     AND suppressed_by = '{}' ORDER BY updated_at DESC LIMIT 1""",
            (chat_id, rel)).fetchone()
    if row and row[1] == sha and not force:
        return "unchanged"
    parsed = parse_note(raw.decode("utf-8", errors="replace"), rel)
    if len(parsed["body"]) < 12:
        if not is_person_note(parsed, rel):
            return "skipped"
        # An EMPTY person-note still names a person (Obsidian shows the
        # filename as the title; the owner's first one had no body yet,
        # live 2026-09-02) — register them, index the name.
        parsed["body"] = parsed["title"]
    from kyraan.store.episodes import sensitivity_flags
    # The exposure decision must come BEFORE the tagging call, not after
    # it (audit 2026-09-03: a note in a local-only folder had its body
    # sent to the cloud tier for tagging, then was stamped local_only).
    # Folder and title hints are known up front; those notes tag locally.
    hint_text = (parsed["title"] + " " + " ".join(parsed["tags"])).lower()
    pre_local = (_under(rel, local_only_folders())
                 or any(h in hint_text for h in _SENSITIVE_HINTS))
    flags = sensitivity_flags(parsed["body"],
                              exposure="local_only" if pre_local else "cloud_ok")
    chunks = chunk_note(parsed["body"])
    if not chunks:
        # all skeleton: findable by title (FTS), invisible to the mesh
        chunks, vectors = [parsed["title"]], [None]
        log_event("note_skeleton_unembedded", path=rel)
    else:
        try:
            vectors = embed.embed(chunks)
        except Exception:
            vectors = [None] * len(chunks)
    doc_id = _note_uuid(chat_id, rel, sha)
    people = link_people(parsed, rel)
    entities = sorted(set(parsed["links"] + [f"#{t}" for t in parsed["tags"]]))
    note_type = str(parsed["meta"].get("type", "")).strip().lower()
    if note_type:
        # typed notes (person/place/event/project/asset/milestone) carry
        # their type as an entity — a filterable kind for later structure
        entities = sorted(set(entities + [f"type:{note_type}"]))
    if is_person_note(parsed, rel):
        pid = register_person_note(parsed, rel)
        if pid and pid not in people:
            people = sorted(people + [pid])
        rel_to = str(parsed["meta"].get("relation", "")).strip()
        if rel_to:
            entities = sorted(set(entities + [f"relation:{rel_to.lower()}"]))
    when = event_date_of(parsed, path.stat().st_mtime)
    with pg.connection() as conn:
        if row:
            conn.execute(
                "UPDATE document SET suppressed_by = ARRAY[%s::uuid] "
                "WHERE id = %s", (SUPERSEDED, row[0]))
        conn.execute(
            """INSERT INTO document (id, chat_id, kind, caption, filename, text,
                                     flags, exposure, subject_persons,
                                     file_path, file_sha256, source_path,
                                     entities, event_date, updated_at)
               VALUES (%s, %s, 'note', %s, %s, %s, %s, %s, %s, '', %s, %s, %s,
                       %s, now())
               ON CONFLICT (id) DO UPDATE SET
                   suppressed_by = '{}', caption = EXCLUDED.caption,
                   text = EXCLUDED.text, flags = EXCLUDED.flags,
                   exposure = EXCLUDED.exposure,
                   subject_persons = EXCLUDED.subject_persons,
                   entities = EXCLUDED.entities,
                   event_date = EXCLUDED.event_date, updated_at = now()""",
            (doc_id, chat_id, parsed["title"], rel, parsed["body"], flags,
             _exposure(parsed, flags, rel), people, sha, rel, entities, when))
        conn.execute("DELETE FROM document_chunk WHERE document_id = %s", (doc_id,))
        for seq, (chunk, vector) in enumerate(zip(chunks, vectors)):
            conn.execute(
                """INSERT INTO document_chunk (id, document_id, seq, text, embedding)
                   VALUES (%s, %s, %s, %s, %s)""",
                (str(uuid.uuid4()), doc_id, seq, chunk,
                 json.dumps(vector) if vector else None))
        conn.commit()
    log_event("note_indexed", path=rel, people=people, chunks=len(chunks),
              exposure=_exposure(parsed, flags, rel))
    try:
        # the note written AFTER the photo still finds it (symmetric)
        from kyraan.store import documents as _documents
        _documents.relate(doc_id)
    except Exception as exc:
        log_event("documents_relate_failed", error=str(exc)[:100])
    return "indexed"


def sync(chat_id: int, root: Path | None = None, force: bool = False) -> dict:
    """Walk the vault; index changed notes; suppress deleted ones."""
    root = root or vault_root()
    if root is None:
        return {"error": "KYRAAN_VAULT_ROOT is not set or not a directory"}
    counts = {"indexed": 0, "unchanged": 0, "skipped": 0, "removed": 0}
    folders = indexed_folders()
    if not folders:
        return {**counts, "error": "KYRAAN_VAULT_FOLDERS is empty — name the "
                                  "folders to index; the vault is never "
                                  "indexed whole"}
    seen = set()
    for path in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue  # .obsidian/, .trash/
        rel = str(path.relative_to(root))
        if not _under(rel, folders):
            continue  # outside the allowlist: does not exist for Kyraan
        seen.add(rel)
        try:
            counts[index_file(chat_id, root, path, force=force)] += 1
        except Exception as exc:
            log_event("note_index_failed", path=rel, error=str(exc)[:120])
            counts["skipped"] += 1
    with pg.connection() as conn:
        rows = conn.execute(
            """SELECT id, source_path FROM document
               WHERE chat_id = %s AND kind = 'note' AND suppressed_by = '{}'""",
            (chat_id,)).fetchall()
        gone = [r[0] for r in rows if r[1] not in seen]
        for doc_id in gone:
            conn.execute(
                "UPDATE document SET suppressed_by = ARRAY[%s::uuid] "
                "WHERE id = %s", (DELETED, doc_id))
        conn.commit()
    counts["removed"] = len(gone)
    try:
        counts["wikilinks"] = link_wikilinks(chat_id)
    except Exception as exc:
        log_event("wikilink_pass_failed", error=str(exc)[:120])
    log_event("vault_synced", **counts)
    return counts


def link_wikilinks(chat_id: int) -> int:
    """Obsidian's own hard edges (owner 2026-09-03: "add wikilink
    indexing"). A note's [[Title]] targets are its `links` entities;
    after a sync every live note's links are resolved against the other
    live notes — by title, or by the file's stem — and stored in
    `related` both ways, the same field a capture uses for the note it
    illustrates. The graph draws note<->note related pairs as wikilinks.
    Returns how many new pairs were made. Links that resolve to nothing
    (a note outside the allowlist, or not written yet) are left alone."""
    with pg.connection() as conn:
        rows = conn.execute(
            """SELECT id, caption, source_path, entities, related FROM document
               WHERE chat_id = %s AND kind = 'note' AND suppressed_by = '{}'""",
            (chat_id,)).fetchall()
        by_title: dict = {}
        for doc_id, caption, source_path, _e, _r in rows:
            by_title[str(caption or "").strip().lower()] = doc_id
            by_title[Path(str(source_path or "")).stem.strip().lower()] = doc_id
        made = 0
        for doc_id, caption, _sp, entities, related in rows:
            have = {str(r) for r in (related or [])}
            targets = [e for e in (entities or [])
                       if not str(e).startswith(("#", "type:", "relation:"))]
            for target in targets:
                other = by_title.get(str(target).strip().lower())
                if other is None or other == doc_id or str(other) in have:
                    continue
                for a, b in ((doc_id, other), (other, doc_id)):
                    conn.execute(
                        """UPDATE document
                           SET related = (SELECT coalesce(array_agg(DISTINCT r), '{}')
                                          FROM unnest(related || %s::uuid[]) r)
                           WHERE id = %s""", ([str(b)], a))
                have.add(str(other))
                made += 1
                log_event("wikilink_linked", note=str(doc_id), target=str(other),
                          title=str(target)[:60])
        conn.commit()
    return made
