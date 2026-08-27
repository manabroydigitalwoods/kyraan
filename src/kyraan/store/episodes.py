"""Episodic memory writer (P3.3b, arch §2.1/§3): chunk a day's
transcript into ~10-exchange episodes, tag sensitivity LOCALLY, embed
LOCALLY, insert into Postgres.

Privacy is structural, not behavioral:
- Episode text is built ONLY from each record's `cloud_text` twin (or
  the legacy placeholder for pre-twin assistant records) — the same
  selection session-seeding and self-review use. Raw `text` on a record
  that carries a twin is never read.
- Sensitivity tagging runs on the LOCAL cheap tier; a tagging failure
  tags the episode `sensitive` (absence discipline beats leakage) and
  logs. Embedding is store/embed.py — local-only by refusal.

Idempotency: episode id = uuid5(chat_id, first record ts). Chunks are
built forward from the day's start, so a chunk's FIRST ts never changes
as later messages arrive — re-ingesting a day upserts the same rows,
and a partial trailing chunk grows in place on the next run.
"""
import json
import uuid
from datetime import datetime

from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.logging_setup import log_event
from kyraan.store import embed, pg

EPISODE_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "episodes.kyraan.local")
EXCHANGES_PER_EPISODE = 10
_SENSITIVE_FLAGS = ("health", "safety", "emotional", "sensitive")

_TAG_SYSTEM = """You label a private conversation snippet with sensitivity flags.
Reply ONLY with JSON: {"flags": [...]} using ONLY these flags, empty list if none apply:
- "health": illness, symptoms, medication, medical visits
- "safety": danger, accidents, emergencies
- "emotional": grief, conflict, fear, distress, relationship strain
- "sensitive": finances, legal matters, private family matters, anything the
  person would not want volunteered in unrelated conversation
No other keys, no prose."""


def cloud_line(entry: dict) -> str | None:
    """The model-safe text for one chat.jsonl record — the privacy-twin
    rule enforced by construction (arch §2.1). None = skip the record."""
    role = entry.get("role")
    if role == "proactive":
        role = "assistant"
    if role not in ("user", "assistant"):
        return None
    text = entry.get("cloud_text") or entry.get("text") or ""
    if role == "assistant" and "cloud_text" not in entry:
        from kyraan.agents.session import _legacy_cloud_placeholder
        text = _legacy_cloud_placeholder(text) or text
    text = text.strip()
    return f"{role}: {text}" if text else None


def chunk_day(records: list) -> dict:
    """{chat_id: [chunk, ...]} where each chunk is a list of (ts, line)
    covering ~EXCHANGES_PER_EPISODE user turns. Records must be one
    day's, in log order."""
    per_chat: dict = {}
    for entry in records:
        line = cloud_line(entry)
        ts = entry.get("ts")
        chat_id = entry.get("chat_id")
        if line is None or not ts or chat_id is None:
            continue
        chunks = per_chat.setdefault(chat_id, [[]])
        exchanges = sum(1 for _, l in chunks[-1] if l.startswith("user:"))
        if exchanges >= EXCHANGES_PER_EPISODE and line.startswith("user:"):
            chunks.append([])  # a new episode starts on a user turn
        chunks[-1].append((ts, line))
    return {chat: [c for c in chunks if c] for chat, chunks in per_chat.items()}


def episode_uuid(chat_id: int, first_ts: str) -> str:
    return str(uuid.uuid5(EPISODE_NS, f"{chat_id}:{first_ts}"))


def sensitivity_flags(text: str) -> list:
    """LOCAL cheap-tier tagging. Failure = tagged 'sensitive' — the
    discretion rules (§3) then keep it out of unrelated answers."""
    from kyraan.model_router import router
    try:
        response = router.call(prompt=text[:4000], system=_TAG_SYSTEM,
                               tier="cheap", force_json=True, max_tokens=128)
        flags = json.loads(response.text).get("flags") or []
        return sorted(set(f for f in flags if f in _SENSITIVE_FLAGS))
    except Exception as exc:
        log_event("episode_tagging_failed", error=str(exc)[:150])
        return ["sensitive"]


def _participants(conn, chat_id: int) -> list:
    row = conn.execute("SELECT id FROM person WHERE chat_id = %s",
                       (chat_id,)).fetchone()
    person = row[0] if row else "owner"
    return sorted({person, "owner"})


def ingest_day(day: str, records: list, tag=None) -> dict:
    """Chunk → tag → embed → upsert one day's records. Returns counts.
    `tag` is injectable for tests; defaults to the local model pass."""
    tag = tag or sensitivity_flags
    chunks_by_chat = chunk_day(records)
    texts, meta = [], []
    for chat_id, chunks in chunks_by_chat.items():
        for chunk in chunks:
            body = "\n".join(line for _, line in chunk)
            texts.append(body)
            meta.append((chat_id, chunk[0][0], body))
    if not texts:
        return {"episodes": 0}
    vectors = embed.embed(texts)
    written = 0
    with pg.connection() as conn:
        for (chat_id, first_ts, body), vector in zip(meta, vectors):
            participants = _participants(conn, chat_id)
            conn.execute(
                """INSERT INTO episode (id, chat_id, day, participants,
                                        visibility, exposure, flags, text,
                                        embedding, created_at)
                   VALUES (%s, %s, %s, %s, 'owner', 'cloud_ok', %s, %s,
                           %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                       text = EXCLUDED.text,
                       flags = EXCLUDED.flags,
                       embedding = EXCLUDED.embedding""",
                (episode_uuid(chat_id, first_ts), chat_id, day, participants,
                 tag(body), body, json.dumps(vector),
                 datetime.fromisoformat(first_ts)))
            written += 1
        conn.commit()
    return {"episodes": written, "chats": len(chunks_by_chat)}


# --- forget cascade (P3.3d, arch §3 / audit P1) ---------------------------

def suppress_for_fact(fact_id: str, fact_content: str) -> int:
    """Sweep after a fact is forgotten: every episode that references the
    fact (fact_refs) or word-overlaps its content (the arch's fixed
    overlap≥2 rule — the same threshold that governs fact matching) gets
    `suppressed_by += fact_id`. Over-sweeping errs safe: a forgotten
    topic must never resurface through recall. Idempotent — an already-
    marked episode is skipped. Returns the number swept."""
    from kyraan.memory.engine import _words
    words = _words(fact_content)
    need = 2 if len(words) >= 2 else 1
    swept = 0
    with pg.connection() as conn:
        rows = conn.execute(
            "SELECT id, text, fact_refs, suppressed_by FROM episode").fetchall()
        for eid, text, fact_refs, suppressed in rows:
            if fact_id in [str(u) for u in (suppressed or [])]:
                continue
            hit = fact_id in [str(u) for u in (fact_refs or [])]
            if not hit:
                hit = len(words & _words(text)) >= need
            if hit:
                conn.execute(
                    """UPDATE episode
                       SET suppressed_by = suppressed_by || %s::uuid
                       WHERE id = %s""", (fact_id, eid))
                swept += 1
        conn.commit()
    return swept


def delete_person_episodes(person_id: str) -> int:
    """A person's delete-me (§1): their episodes are hard-deleted by
    participant, not suppressed — deletion means gone."""
    with pg.connection() as conn:
        deleted = conn.execute(
            "DELETE FROM episode WHERE %s = ANY(participants)",
            (person_id,)).rowcount
        conn.commit()
    log_event("person_episodes_deleted", person=person_id, episodes=deleted)
    return deleted


# --- recall (P3.3c) -------------------------------------------------------

_DISCRETION_FLAGS = {"emotional", "sensitive"}
RECALL_K_MAX = 8


def recall(chat_id: int, query: str, k: int = 5) -> list:
    """Hybrid ANN+FTS recall over this chat's episodes, recency-biased,
    top-k. Discretion is applied POST-retrieval (arch §3): an episode
    flagged emotional/sensitive surfaces only on a direct FTS hit —
    semantic adjacency alone never volunteers it. Suppressed episodes
    (forget cascade, P3.3d) are excluded at the source."""
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 5
    k = max(1, min(k, RECALL_K_MAX))
    qvec = json.dumps(embed.embed([query])[0])
    import re as _re
    from kyraan.memory.engine import _words
    terms = [w for w in _words(query) if _re.fullmatch(r"[a-z0-9]+", w)]
    tsquery = " | ".join(terms)
    with pg.connection() as conn:
        rows = conn.execute(
            """SELECT day, flags, text,
                      1 - (embedding <=> %s::vector) AS sim,
                      (%s <> '' AND to_tsvector('english', text)
                                    @@ to_tsquery('english', %s)) AS fts
               FROM episode
               WHERE chat_id = %s AND suppressed_by = '{}'
               ORDER BY embedding <=> %s::vector
               LIMIT 40""",
            (qvec, tsquery, tsquery or "x", chat_id, qvec)).fetchall()
    today = local_now().date()
    scored = []
    for day, flags, text, sim, fts in rows:
        if _DISCRETION_FLAGS & set(flags or []) and not fts:
            continue  # absence discipline, not a lower rank
        age_days = max((today - day).days, 0)
        score = float(sim) + (0.15 if fts else 0.0) + 0.1 / (1 + age_days / 30)
        scored.append((score, day, text))
    scored.sort(key=lambda item: -item[0])
    lines, seen = [], set()
    for _score, day, text in scored:
        head = text[:120]  # repeated sequences (eval reruns, retries)
        if head in seen:   # produce near-identical episodes — one is enough
            continue
        seen.add(head)
        lines.append(f"[recalled from {day.isoformat()}] {text[:500]}")
        if len(lines) >= k:
            break
    return lines


def records_for_day(day: str, lines: list) -> list:
    """Filter parsed chat.jsonl records to one local-calendar day."""
    tz = local_now().tzinfo
    keep = []
    for entry in lines:
        try:
            when = datetime.fromisoformat(entry["ts"]).astimezone(tz)
        except (KeyError, ValueError):
            continue
        if when.date().isoformat() == day:
            keep.append(entry)
    return keep


def ingest_recent(days: list) -> dict:
    """The nightly entry point: (re-)ingest the given local days from
    chat.jsonl — idempotent, so a nightly yesterday+today pass covers the
    late-evening messages the previous run missed."""
    from kyraan.control_plane import logging_setup
    path = logging_setup.CHAT_LOG
    if not path.exists():
        return {"episodes": 0}
    parsed = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    totals = {"episodes": 0}
    for day in days:
        result = ingest_day(day, records_for_day(day, parsed))
        totals["episodes"] += result.get("episodes", 0)
    log_event("episodes_ingested", days=days, **totals)
    return totals
