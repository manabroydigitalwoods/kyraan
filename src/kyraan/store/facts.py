"""Fact mirroring: memory/index.json → Postgres (P3.2a, arch §2.3).

Files remain the write/review authority (arch §2.1). Every index
mutation calls `mirror_entries` AFTER the file write; PG being down
logs `fact_sync_deferred` and never blocks or fails the file op —
`scripts/resync_facts.py` rebuilds the table from the index at any
time, idempotently, because identity is deterministic:
`id = uuid5(KYRAAN_NS, legacy_id)`.

Subject derivation (audit P1 — a blanket subject='owner' would mis-own
family facts): a `people/<name>` target claims subject `<name>` when a
person row with that id exists; `people/owner.md` is the owner; any
other people/ fact is UNRESOLVED — stored subject='owner' with
subject_reviewed=false, listed by `scripts/review_subjects.py`, and
P3.5a's gate refuses non-owner viewers while one remains. Facts under
routines/preferences/work are structurally the owner's own.
"""
import time
import uuid
from datetime import datetime, timezone

from kyraan.control_plane.logging_setup import log_event
from kyraan.store import pg

KYRAAN_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "facts.kyraan.local")
OWNER = "owner"

# Tests flip this off (conftest autouse) so no unit test can ever write
# into the live container; the pg-marked sync tests re-enable it.
MIRROR_ENABLED = True

# Circuit breaker: after a failure, skip attempts for a minute so a down
# PG costs one 5s pool timeout, not one per message.
_BREAKER_S = 60
_breaker_until = 0.0


def fact_uuid(legacy_id: str) -> str:
    return str(uuid.uuid5(KYRAAN_NS, legacy_id))


def subject_for(entry: dict, persons: set) -> tuple[str, bool]:
    """(subject, subject_reviewed) for an index entry."""
    target = str(entry.get("target", ""))
    if target.startswith("people/"):
        name = target.split("/", 1)[1].removesuffix(".md")
        if name == OWNER or name in persons:
            return (OWNER if name == OWNER else name, True)
        return (OWNER, False)  # unresolved — the review queue owns it
    return (OWNER, True)


def seed_owner(conn) -> None:
    conn.execute(
        "INSERT INTO person (id, stage) VALUES (%s, 'owner') "
        "ON CONFLICT (id) DO NOTHING", (OWNER,))


def _upsert(conn, entry: dict, persons: set) -> None:
    subject, reviewed = subject_for(entry, persons)
    try:
        created = datetime.fromisoformat(entry["created"])
    except (KeyError, ValueError):
        created = datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO fact (id, legacy_id, subject, subject_reviewed, owner,
                             content, kind, flags, era, sphere, visibility,
                             exposure, active, created_at, source_msg)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   'owner', 'cloud_ok', %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET
               content = EXCLUDED.content,
               kind = EXCLUDED.kind,
               flags = EXCLUDED.flags,
               era = EXCLUDED.era,
               sphere = EXCLUDED.sphere,
               active = EXCLUDED.active,
               -- an owner-reviewed subject is never clobbered by a resync
               subject = CASE WHEN fact.subject_reviewed THEN fact.subject
                              ELSE EXCLUDED.subject END,
               subject_reviewed = fact.subject_reviewed
                                  OR EXCLUDED.subject_reviewed""",
        (fact_uuid(entry["id"]), entry["id"], subject, reviewed, OWNER,
         entry["content"], entry.get("kind", "other"),
         sorted(entry.get("flags") or []),
         entry.get("era"), entry.get("sphere"), bool(entry.get("active")),
         created, str(entry.get("source", ""))[:500]))


def sync_entries(conn, entries: list) -> None:
    """Two passes: rows first, then superseded_by links — the link's FK
    target may be later in the batch (or, defensively, absent)."""
    seed_owner(conn)
    persons = {r[0] for r in conn.execute("SELECT id FROM person")}
    for entry in entries:
        _upsert(conn, entry, persons)
    for entry in entries:
        new_legacy = entry.get("superseded_by")
        conn.execute(
            """UPDATE fact SET superseded_by =
                   (SELECT id FROM fact WHERE legacy_id = %s)
               WHERE legacy_id = %s""", (new_legacy, entry["id"]))


def mirror_entries(entries: list) -> bool:
    """Best-effort mirror of changed index entries. Returns True when the
    rows landed; on any failure logs fact_sync_deferred and returns False
    — the caller's file write has already succeeded and stands."""
    global _breaker_until
    if not MIRROR_ENABLED or not entries:
        return False
    if time.monotonic() < _breaker_until:
        log_event("fact_sync_deferred", entries=len(entries), reason="breaker open")
        return False
    try:
        with pg.connection() as conn:
            sync_entries(conn, entries)
            conn.commit()
        return True
    except Exception as exc:
        _breaker_until = time.monotonic() + _BREAKER_S
        log_event("fact_sync_deferred", entries=len(entries),
                  reason=str(exc)[:200])
        return False
