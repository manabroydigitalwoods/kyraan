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


def list_persons() -> list:
    with pg.connection() as conn:
        return conn.execute(
            "SELECT id, chat_id, stage, consented_at FROM person ORDER BY id"
        ).fetchall()
