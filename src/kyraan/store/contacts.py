"""Contact store (2026-09-01): the local half of the Contacts sync.
Names resolve; phones/emails leave this module ONLY through the
contacts.find direct-reply composer — never toward a model prompt."""
from kyraan.control_plane.logging_setup import log_event
from kyraan.store import pg


def upsert_all(contacts: list) -> int:
    with pg.connection() as conn:
        for c in contacts:
            conn.execute(
                """INSERT INTO contact (resource, name, phones, emails,
                                        updated_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (resource) DO UPDATE
                   SET name = EXCLUDED.name, phones = EXCLUDED.phones,
                       emails = EXCLUDED.emails, updated_at = now()""",
                (c["resource"], c["name"], c["phones"], c["emails"]))
        conn.commit()
    log_event("contacts_synced", count=len(contacts))
    return len(contacts)


def find(name: str, limit: int = 5) -> list:
    """[{name, phones, emails}] whose name contains every query word."""
    words = [w for w in name.lower().split() if w]
    if not words:
        return []
    with pg.connection() as conn:
        rows = conn.execute(
            "SELECT name, phones, emails FROM contact "
            "ORDER BY lower(name)").fetchall()
    out = []
    for nm, phones, emails in rows:
        hay = nm.lower()
        if all(w in hay for w in words):
            out.append({"name": nm, "phones": list(phones or []),
                        "emails": list(emails or [])})
            if len(out) >= limit:
                break
    return out
