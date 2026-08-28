"""Person identity for multi-user (P3.5a, arch §4 / governance §8).

Enrollment is EXPLICIT and owner-run: a person row with chat_id, stage
(none|read_mostly|full), and consent date. The channel admits "an
enrolled private chat at stage >= read_mostly"; unknown chats stay
rejected exactly as before Phase 3.

The enablement gate (audit P1): enrolling any non-owner person REFUSES
while an ACTIVE fact has subject_reviewed=false — every fact's owner
must be settled before a second viewer can exist.
"""
import time

from kyraan.control_plane.logging_setup import log_event
from kyraan.store import pg

STAGES = ("none", "read_mostly", "full")
_ADMITTED_STAGES = ("read_mostly", "full", "owner")

# Per-message person resolution hits PG; a tiny TTL cache keeps the
# hot path off the pool. Fail-closed: PG down → non-owner rejected
# (the owner's env-based gate never touches PG).
_cache: dict = {}
_CACHE_TTL_S = 60


def name_map() -> dict:
    """Every name that resolves to a person: {'kamal': 'kamal', 'titu':
    'titu_roy', 'maan': 'owner', 'titu roy': 'titu_roy', ...} — ids,
    ids-with-spaces, and stored aliases, all lowercased. TTL-cached like
    chat resolution; fail-open to empty (resolution is enrichment, never
    a gate)."""
    now = time.monotonic()
    cached = _cache.get("__name_map__")
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]
    mapping: dict = {}
    try:
        with pg.connection() as conn:
            rows = conn.execute("SELECT id, aliases FROM person").fetchall()
        for pid, aliases in rows:
            mapping[pid] = pid
            mapping[pid.replace("_", " ")] = pid
            for alias in aliases or []:
                mapping[alias.strip().lower()] = pid
    except Exception:
        return {}
    _cache["__name_map__"] = (now, mapping)
    return mapping


def resolve(name: str) -> str | None:
    """The one deterministic name -> person-id join, or None for a name
    the registry doesn't know (free-form graph nodes stay free-form)."""
    return name_map().get(str(name or "").strip().lower().replace("-", "_")) \
        or name_map().get(str(name or "").strip().lower().replace("_", " "))


def add_alias(person_id: str, alias: str) -> None:
    alias = alias.strip().lower()
    if not alias:
        return
    with pg.connection() as conn:
        conn.execute(
            """UPDATE person SET aliases = (
                   SELECT coalesce(array_agg(DISTINCT a), '{}')
                   FROM unnest(aliases || %s::text[]) a)
               WHERE id = %s""", ([alias], person_id))
        conn.commit()
    _cache.pop("__name_map__", None)


def unreviewed_active_facts() -> int:
    with pg.connection() as conn:
        count, = conn.execute(
            "SELECT count(*) FROM fact WHERE active AND NOT subject_reviewed"
        ).fetchone()
    return count


def enroll(person_id: str, chat_id: int, stage: str, consented: str) -> None:
    """The owner-run ceremony. Raises on any gate violation."""
    person_id = person_id.strip().lower()
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    if person_id == "owner":
        raise ValueError("the owner is not enrolled — the owner IS the gate")
    pending = unreviewed_active_facts()
    if pending and stage != "none":
        raise ValueError(
            f"REFUSED: {pending} active fact(s) still have unreviewed "
            "subjects — run scripts/review_subjects.py first (audit P1: "
            "mis-owned facts must be assigned before a second viewer exists)")
    with pg.connection() as conn:
        conn.execute(
            """INSERT INTO person (id, chat_id, stage, consented_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   chat_id = EXCLUDED.chat_id,
                   stage = EXCLUDED.stage,
                   consented_at = EXCLUDED.consented_at""",
            (person_id, chat_id, stage, consented))
        conn.commit()
    _cache.clear()
    log_event("person_enrolled", person=person_id, stage=stage,
              consented=consented)


def person_id_any_stage(chat_id: int) -> str | None:
    """The person id for a chat REGARDLESS of stage — the identity
    layer's lookup (2026-08-28: the channel set stage only, the empty
    viewer defaulted to owner, and Ruma's chat called HER Maan and told
    her she was the owner). Admission is a separate question answered
    by person_for_chat; identity must be right even for stage none."""
    now = time.monotonic()
    cached = _cache.get(("any", chat_id))
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]
    try:
        with pg.connection() as conn:
            row = conn.execute(
                "SELECT id FROM person WHERE chat_id = %s",
                (chat_id,)).fetchone()
        result = row[0] if row else None
    except Exception:
        result = None  # fail-closed: unknown, never owner
    _cache[("any", chat_id)] = (now, result)
    return result


def person_for_chat(chat_id: int) -> tuple | None:
    """(person_id, stage) for an ADMITTED enrolled chat, else None.
    Fail-closed on any store trouble."""
    now = time.monotonic()
    cached = _cache.get(chat_id)
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]
    try:
        with pg.connection() as conn:
            row = conn.execute(
                "SELECT id, stage FROM person WHERE chat_id = %s",
                (chat_id,)).fetchone()
    except Exception as exc:
        log_event("person_lookup_failed", chat_id=chat_id,
                  error=str(exc)[:120])
        return None
    result = (row[0], row[1]) if row and row[1] in _ADMITTED_STAGES else None
    _cache[chat_id] = (now, result)
    return result


def extraction_enabled(person_id: str) -> bool:
    """The §4 first-month rule: extraction from a non-owner's messages
    is OFF unless explicitly enabled per person. Fail-closed."""
    if person_id == "owner":
        return True
    try:
        with pg.connection() as conn:
            row = conn.execute(
                "SELECT extraction_enabled FROM person WHERE id = %s",
                (person_id,)).fetchone()
        return bool(row and row[0])
    except Exception as exc:
        log_event("person_lookup_failed", person=person_id,
                  error=str(exc)[:120])
        return False


def set_extraction(person_id: str, enabled: bool) -> None:
    with pg.connection() as conn:
        n = conn.execute(
            "UPDATE person SET extraction_enabled = %s WHERE id = %s",
            (enabled, person_id.strip().lower())).rowcount
        conn.commit()
    if not n:
        raise ValueError(f"no person {person_id!r}")
    log_event("person_extraction_set", person=person_id, enabled=enabled)


def dnd_window(chat_id: int) -> tuple | None:
    """(start, end) "HH:MM" strings for the person behind this chat, or
    None (no person / no window / store trouble — global DND still
    applies either way)."""
    try:
        with pg.connection() as conn:
            row = conn.execute(
                "SELECT dnd_start, dnd_end FROM person WHERE chat_id = %s",
                (chat_id,)).fetchone()
        if row and row[0] and row[1]:
            return (row[0], row[1])
        return None
    except Exception:
        return None


def daily_budget(person_id: str) -> float:
    """The person's daily model-spend cap in USD. Unset column → a
    conservative default; owner is uncapped here (the global budget
    still applies to everyone)."""
    try:
        with pg.connection() as conn:
            row = conn.execute(
                "SELECT daily_budget_usd FROM person WHERE id = %s",
                (person_id,)).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception:
        pass
    return 0.50  # fail-closed-ish: an unconfigured person gets a small cap


def list_persons() -> list:
    with pg.connection() as conn:
        return conn.execute(
            "SELECT id, chat_id, stage, consented_at FROM person ORDER BY id"
        ).fetchall()
