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
Reply ONLY with JSON: {"flags": [...]}. The ONLY strings allowed inside
"flags" are exactly "health", "safety", "emotional", "sensitive" — never
invent other labels and never use a topic word below as a label. Empty
list if none apply.
- "health": illness, symptoms, medication, medical visits
- "safety": danger, accidents, emergencies
- "emotional": grief, conflict, fear, distress, relationship strain
- "sensitive": money or finances, legal matters, private family matters,
  anything the person would not want volunteered in unrelated conversation
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


SESSION_GAP_MINUTES = 30  # a silence this long ends the sitting


def chunk_day(records: list) -> dict:
    """{chat_id: [chunk, ...]} where each chunk is a list of (ts, line).
    RAG precision (2026-08-27): a chunk is ONE SITTING — it splits on a
    >30min silence as well as on the ~EXCHANGES_PER_EPISODE cap, so an
    episode's embedding points at one topic instead of averaging three
    (a morning weather check and an evening health worry used to share
    one vector). Records must be one day's, in log order. Splits stay
    FORWARD-deterministic: boundaries depend only on earlier records, so
    a chunk's first ts — its identity — never changes as the day grows."""
    per_chat: dict = {}
    for entry in records:
        line = cloud_line(entry)
        ts = entry.get("ts")
        chat_id = entry.get("chat_id")
        if line is None or not ts or chat_id is None:
            continue
        chunks = per_chat.setdefault(chat_id, [[]])
        current = chunks[-1]
        split = False
        if current and line.startswith("user:"):
            exchanges = sum(1 for _, l in current if l.startswith("user:"))
            if exchanges >= EXCHANGES_PER_EPISODE:
                split = True
            else:
                try:
                    gap = (datetime.fromisoformat(ts)
                           - datetime.fromisoformat(current[-1][0]))
                    split = gap.total_seconds() > SESSION_GAP_MINUTES * 60
                except ValueError:
                    pass
        if split:
            chunks.append([])
        chunks[-1].append((ts, line))
    return {chat: [c for c in chunks if c] for chat, chunks in per_chat.items()}


def episode_uuid(chat_id: int, first_ts: str) -> str:
    return str(uuid.uuid5(EPISODE_NS, f"{chat_id}:{first_ts}"))


# Pinned by scripts/probe_tagger.py (re-probed 2026-08-27, 16 labeled
# probes, misses are the axis that decides): ministral-3:3b's one miss
# is a strict subset of llama3.2:3b's two — it catches the gas-smell
# safety flag llama3.2 drops — at comparable latency (~395ms vs 305ms)
# and the same size class, so it took the fallback pin. Neither 3B is
# miss-free (both drop `health` on "reminder for Kiaan's vaccination");
# that's why nano stays primary and this model only tags non-cloud_ok
# episodes and cloud-failure fallbacks. qwen3:8b is the only miss-free
# local model if this pin ever needs a safer (slower) replacement.
TAG_MODEL = "ministral-3:3b"


def _tag_chat(text: str) -> list:
    """One local tagging call — same local-only endpoint guard as the
    embedder (episode text never leaves this machine)."""
    import urllib.request
    request = urllib.request.Request(
        f"{embed._endpoint()}/api/chat",
        data=json.dumps({
            "model": TAG_MODEL, "stream": False, "format": "json",
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": _TAG_SYSTEM},
                         {"role": "user", "content": text[:4000]}],
        }).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as resp:
        payload = json.loads(resp.read())
    return json.loads(payload["message"]["content"]).get("flags") or []


# Models paraphrase the prompt's TOPIC words as labels ("finances",
# "legal matters", "grief") no matter how firmly the prompt forbids it —
# nano did it nondeterministically across two probe runs. Deterministic
# normalization beats prompt whack-a-mole (the deflection-guard lesson):
# a paraphrase maps to its parent flag; true junk still drops.
_FLAG_STEMS = {
    "health": ("illness", "symptom", "medication", "medical"),
    "safety": ("danger", "accident", "emergenc"),
    "emotional": ("grief", "conflict", "fear", "distress", "relationship"),
    "sensitive": ("money", "financ", "legal", "family matter", "private"),
}


def normalize_flags(raw: list) -> list:
    out = set()
    for label in raw:
        label = str(label).strip().lower().lstrip("_")
        if label in _SENSITIVE_FLAGS:
            out.add(label)
            continue
        for flag, stems in _FLAG_STEMS.items():
            if any(stem in label for stem in stems):
                out.add(flag)
                break
    return sorted(out)


def sensitivity_flags(text: str, exposure: str = "cloud_ok") -> list:
    """Tagging: nano (frontier) for cloud_ok text — episode text is
    built from cloud_text twins, already cloud-safe — with the LOCAL
    TAG_MODEL path for any non-cloud_ok episode AND as fallback when the
    cloud fails; total failure = 'sensitive' (§3 absence discipline)."""
    if exposure == "cloud_ok":
        try:
            from kyraan.model_router import router
            response = router.call(prompt=text[:4000], system=_TAG_SYSTEM,
                                   tier="frontier", force_json=True,
                                   max_tokens=128)
            return normalize_flags(json.loads(response.text).get("flags") or [])
        except Exception as exc:
            log_event("episode_tagging_cloud_failed", error=str(exc)[:150])
    try:
        return normalize_flags(_tag_chat(text))
    except Exception as exc:
        log_event("episode_tagging_failed", error=str(exc)[:150])
        return ["sensitive"]


def _participants(conn, chat_id: int) -> list:
    row = conn.execute("SELECT id FROM person WHERE chat_id = %s",
                       (chat_id,)).fetchone()
    person = row[0] if row else "owner"
    return sorted({person, "owner"})


def ingest_day(day: str, records: list, tag=None,
               skip_unchanged: bool = False) -> dict:
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
    if skip_unchanged:
        # Same-day catch-up (2026-08-28): every pass re-tagged and
        # re-embedded EVERY chunk (a nano call each) — a half-hourly job
        # must touch only chunks whose text actually grew. Chunk ids are
        # deterministic (chat, first_ts), so identical text = no work.
        wanted = [episode_uuid(c, ts) for c, ts, _ in meta]
        with pg.connection() as conn:
            rows = conn.execute(
                "SELECT id, text FROM episode WHERE id = ANY(%s)",
                (wanted,)).fetchall()
        unchanged = {str(i) for i, t in rows}
        existing_text = {str(i): t for i, t in rows}
        keep = [k for k, (c, ts, body) in enumerate(meta)
                if episode_uuid(c, ts) not in unchanged
                or existing_text[episode_uuid(c, ts)] != body]
        if not keep:
            return {"episodes": 0}
        texts = [texts[k] for k in keep]
        meta = [meta[k] for k in keep]
    vectors = embed.embed(texts)
    # tagging is a model call per chunk — never inside a held pool
    # connection (review 2026-09-03: a busy day pinned one of four for
    # a minute and hot-path lookups timed out)
    flags_list = [tag(body) for (_c, _t, body) in meta]
    written = 0
    with pg.connection() as conn:
        for (chat_id, first_ts, body), vector, flags in zip(meta, vectors, flags_list):
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
                 flags, body, json.dumps(vector),
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


def _search(chat_id: int, query: str) -> list:
    """The shared retrieval core: this chat's unsuppressed episodes by
    ANN, with FTS hit and raw similarity exposed, discretion applied
    (arch §3: an emotional/sensitive episode surfaces only on a direct
    FTS hit — semantic adjacency alone never volunteers it). Returns
    [(score, sim, day, text)] best-first."""
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
        scored.append((score, float(sim), day, text))
    scored.sort(key=lambda item: -item[0])
    return scored


def recall(chat_id: int, query: str, k: int = 5) -> list:
    """Hybrid ANN+FTS recall over this chat's episodes, recency-biased,
    top-k. Suppressed episodes (forget cascade, P3.3d) are excluded at
    the source; discretion applies in the shared core."""
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 5
    k = max(1, min(k, RECALL_K_MAX))
    scored = [(score, day, text) for score, _sim, day, text
              in _search(chat_id, query)]
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


RAG_MIN_SIM = 0.35   # calibrated 2026-08-27 (probe_rag battery): real
                     # matches measured 0.38-0.50, unrelated noise 0.11
                     # — 0.45 was losing true hits at 0.38/0.43
RAG_MAX_SNIPPETS = 2
RAG_CLIP = 280


def relevant_snippets(chat_id: int, message: str) -> list:
    """True RAG (owner directive 2026-08-27): the top past-conversation
    snippets for THIS message, auto-injected into the loop's context —
    no tool call needed. Same core as recall (suppression, discretion,
    chat scope) plus a similarity floor so unrelated history never
    pollutes the prompt. Empty on any failure — context degrades to
    exactly the pre-RAG prompt."""
    try:
        ranked = _search(chat_id, message)
        picked, seen = [], set()
        for _score, sim, day, text in ranked:
            if sim < RAG_MIN_SIM:
                break  # best-first: everything after is weaker
            head = text[:120]
            if head in seen:
                continue
            seen.add(head)
            clipped = text[:RAG_CLIP] + ("…" if len(text) > RAG_CLIP else "")
            picked.append(f"[from an earlier conversation, {day.isoformat()}] "
                          + clipped.replace("\n", " ⏎ "))
            if len(picked) >= RAG_MAX_SNIPPETS:
                break
        # Observability for tuning: every retrieval logs its best
        # similarity — with these, the 0.45 floor becomes a calibrated
        # number instead of a guess (near-misses show up as
        # injected=0 with best_sim just under the floor).
        log_event("episode_rag", chat_id=chat_id, injected=len(picked),
                  best_sim=round(ranked[0][1], 3) if ranked else None)
        return picked
    except Exception as exc:
        log_event("episode_rag_skipped", reason=str(exc)[:120])
        return []


def records_for_day(day: str, lines: list) -> list:
    """Filter parsed chat.jsonl records to one local-calendar day."""
    tz = local_now().tzinfo
    from kyraan.agents.secrets import apply_redactions
    lines = apply_redactions(lines)          # a secret never becomes an episode
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
        result = ingest_day(day, records_for_day(day, parsed),
                            skip_unchanged=True)
        totals["episodes"] += result.get("episodes", 0)
    log_event("episodes_ingested", days=days, **totals)
    return totals


def catch_up_today() -> dict:
    """Same-day recall (2026-08-28): episodes ingested only at 21:45
    made "what did we discuss this morning?" blind until tonight. A
    half-hourly pass keeps recall within ~30 min of live, touching only
    chunks whose text changed."""
    return ingest_recent([local_now().date().isoformat()])
