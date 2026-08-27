"""P3.5a — enrollment + channel gate: the audit-P1 refusal (no second
viewer while a fact's subject is unreviewed), stage admission, and
_authorized covering owner/enrolled/unknown exactly as specified."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

_env = Path(__file__).resolve().parents[1] / ".env"
if "KYRAAN_PG_DSN" not in os.environ and _env.exists():
    for line in _env.read_text().splitlines():
        if line.startswith("KYRAAN_PG_DSN="):
            os.environ["KYRAAN_PG_DSN"] = line.split("=", 1)[1].strip()
            break

from kyraan.store import persons, pg  # noqa: E402

_PG_UP = pg.available()


@pytest.fixture
def person_db(monkeypatch):
    if not _PG_UP:
        pytest.skip("local Postgres container unreachable")
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    persons._cache.clear()
    with pg.connection() as conn:
        conn.execute("TRUNCATE person, fact CASCADE")
        conn.commit()
    yield
    persons._cache.clear()
    pg.reset_pool_for_tests()


def _seed_unreviewed_fact():
    from kyraan.store import facts
    with pg.connection() as conn:
        facts.sync_entries(conn, [{
            "id": "gatefact1", "content": "Family fact, subject unsettled",
            "target": "people/mystery.md", "kind": "relationship",
            "term": "long", "importance": "normal", "flags": [],
            "era": "current", "sphere": "personal",
            "created": "2026-08-27T00:00:00+00:00", "source": "t",
            "active": True, "superseded_by": None}])
        conn.commit()


@pytest.mark.pg
def test_gate_refuses_enrollment_while_subjects_unreviewed(person_db):
    _seed_unreviewed_fact()
    with pytest.raises(ValueError, match="unreviewed"):
        persons.enroll("ruma", 111222, "read_mostly", "2026-08-27")
    # stage none (revocation shape) passes the gate — it grants nothing
    persons.enroll("ruma", 111222, "none", "2026-08-27")


@pytest.mark.pg
def test_enroll_and_admission_stages(person_db):
    persons.enroll("ruma", 111222, "read_mostly", "2026-08-27")
    persons.enroll("kiaan", 333444, "none", "2026-08-27")
    assert persons.person_for_chat(111222) == ("ruma", "read_mostly")
    assert persons.person_for_chat(333444) is None   # stage none: rejected
    assert persons.person_for_chat(999999) is None   # unknown: rejected
    persons._cache.clear()
    persons.enroll("ruma", 111222, "none", "2026-08-27")  # revoke
    assert persons.person_for_chat(111222) is None


@pytest.mark.pg
def test_owner_is_never_enrollable(person_db):
    with pytest.raises(ValueError, match="owner IS the gate"):
        persons.enroll("owner", 1, "full", "2026-08-27")


def test_person_lookup_fails_closed(monkeypatch):
    def boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(persons.pg, "connection", boom)
    persons._cache.clear()
    assert persons.person_for_chat(111222) is None


# --- the channel gate -----------------------------------------------------

def _update(user_id, chat_id, chat_type="private"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type))


def test_authorized_owner_path_is_env_only(monkeypatch):
    from kyraan.channels import telegram_bot
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "777")

    def boom(chat_id):
        raise AssertionError("the owner path must never touch the store")

    monkeypatch.setattr(persons, "person_for_chat", boom)
    assert telegram_bot._authorized(_update(777, 777)) == "owner"


def test_authorized_enrolled_and_unknown(monkeypatch):
    from kyraan.channels import telegram_bot
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "777")
    monkeypatch.setattr(persons, "person_for_chat",
                        lambda chat_id: ("ruma", "read_mostly") if chat_id == 111222 else None)
    assert telegram_bot._authorized(_update(111222, 111222)) == "ruma"
    assert telegram_bot._authorized(_update(999, 999)) is None


def test_authorized_rejects_groups_even_for_enrolled(monkeypatch):
    from kyraan.channels import telegram_bot
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "777")
    monkeypatch.setattr(persons, "person_for_chat",
                        lambda chat_id: ("ruma", "read_mostly"))
    assert telegram_bot._authorized(_update(111222, -5000, "group")) is None
    # a private chat whose user != chat (forwarded bots etc.) is rejected
    assert telegram_bot._authorized(_update(111222, 111333)) is None