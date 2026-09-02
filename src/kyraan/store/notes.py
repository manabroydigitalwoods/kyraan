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


def parse_note(text: str, rel_path: str) -> dict:
    """Frontmatter (title/tags/date/aliases), body, wikilinks, tags."""
    meta: dict = {}
    body = text
    m = _FRONTMATTER.match(text)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip("\"'")
    title = meta.get("title") or Path(rel_path).stem.replace("_", " ")
    links = [l.strip() for l in _WIKILINK.findall(body) if l.strip()]
    tags = [t.lower() for t in _TAG.findall(body)]
    for raw_tag in str(meta.get("tags", "")).strip("[]").split(","):
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
    return chunks or [body[:CHUNK_CHARS]]


def link_people(parsed: dict) -> list:
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
    for link in parsed["links"]:
        pid = nm.get(link.lower()) or nm.get(link.lower().replace(" ", "_"))
        if pid and pid != "owner" and pid not in found:
            found.append(pid)
    return sorted(found)


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
    pid = _slug(name)
    if not pid or pid == "owner" or len(name) < 2:
        return None
    try:
        existing = {p[0] for p in persons.list_persons()}
        if pid not in existing:
            persons.enroll(pid, None, "none", None)
            log_event("person_registered_from_note", person=pid, path=rel)
        aliases = [a.strip().strip("[]\"'") for a in
                   str(parsed["meta"].get("aliases", "")).strip("[]").split(",")]
        for alias in [name] + aliases:
            if alias and _slug(alias) != pid:
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


def index_file(chat_id: int, root: Path, path: Path) -> str:
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
    if row and row[1] == sha:
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
    flags = sensitivity_flags(parsed["body"])
    chunks = chunk_note(parsed["body"])
    try:
        vectors = embed.embed(chunks)
    except Exception:
        vectors = [None] * len(chunks)
    doc_id = _note_uuid(chat_id, rel, sha)
    people = link_people(parsed)
    entities = sorted(set(parsed["links"] + [f"#{t}" for t in parsed["tags"]]))
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
    return "indexed"


def sync(chat_id: int, root: Path | None = None) -> dict:
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
            counts[index_file(chat_id, root, path)] += 1
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
    log_event("vault_synced", **counts)
    return counts
