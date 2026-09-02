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


def _note_uuid(chat_id: int, rel_path: str, sha: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"note:{chat_id}:{rel_path}:{sha}"))


def _exposure(parsed: dict, flags: list) -> str:
    text = (parsed["title"] + " " + " ".join(parsed["tags"])).lower()
    if flags or any(h in text for h in _SENSITIVE_HINTS):
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
        return "skipped"
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
             _exposure(parsed, flags), people, sha, rel, entities, when))
        conn.execute("DELETE FROM document_chunk WHERE document_id = %s", (doc_id,))
        for seq, (chunk, vector) in enumerate(zip(chunks, vectors)):
            conn.execute(
                """INSERT INTO document_chunk (id, document_id, seq, text, embedding)
                   VALUES (%s, %s, %s, %s, %s)""",
                (str(uuid.uuid4()), doc_id, seq, chunk,
                 json.dumps(vector) if vector else None))
        conn.commit()
    log_event("note_indexed", path=rel, people=people, chunks=len(chunks),
              exposure=_exposure(parsed, flags))
    return "indexed"


def sync(chat_id: int, root: Path | None = None) -> dict:
    """Walk the vault; index changed notes; suppress deleted ones."""
    root = root or vault_root()
    if root is None:
        return {"error": "KYRAAN_VAULT_ROOT is not set or not a directory"}
    counts = {"indexed": 0, "unchanged": 0, "skipped": 0, "removed": 0}
    seen = set()
    for path in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue  # .obsidian/, .trash/
        rel = str(path.relative_to(root))
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
