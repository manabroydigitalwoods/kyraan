"""Google Contacts (governance 2026-09-01): normalization, the
local-only direct-reply boundary, and the sync store."""
import pytest

from kyraan.control_plane import kernel
from kyraan.tools import google_contacts


def test_fetch_normalizes_and_pages(monkeypatch):
    pages = [
        {"connections": [
            {"resourceName": "people/1",
             "names": [{"displayName": "Suman Ghosh"}],
             "phoneNumbers": [{"value": "+91 90000 00001"}],
             "emailAddresses": [{"value": "s@x.com"}]},
            {"resourceName": "people/2", "names": [{}]},   # nameless: skipped
        ], "nextPageToken": "t2"},
        {"connections": [
            {"resourceName": "people/3",
             "names": [{"displayName": "Kamal"}]}]},
    ]
    calls = []
    monkeypatch.setattr(google_contacts, "_api",
                        lambda path: calls.append(path) or pages.pop(0))
    out = google_contacts.fetch_all()
    assert [c["name"] for c in out] == ["Suman Ghosh", "Kamal"]
    assert out[0]["phones"] == ["+91 90000 00001"]
    assert "pageToken=t2" in calls[1]


async def test_find_is_a_direct_reply_numbers_never_in_prompts(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.store import contacts as cstore
    monkeypatch.setattr(google_contacts, "enabled", lambda: True)
    monkeypatch.setattr(cstore, "find", lambda name, limit=5: [
        {"name": "Suman Ghosh", "phones": ["+91 90000 00001"],
         "emails": ["s@x.com"]}])
    out = await loop_tools._contacts_find(7, {"name": "suman"}, "")
    assert "+91 90000 00001" in out["__direct_reply__"]
    assert "90000" not in out["__history__"]        # placeholder only


async def test_find_disabled_is_honest(monkeypatch):
    from kyraan.agents import loop_tools
    monkeypatch.setattr(google_contacts, "enabled", lambda: False)
    with pytest.raises(kernel.ToolFailed, match="KYRAAN_CONTACTS"):
        await loop_tools._contacts_find(7, {"name": "suman"}, "")


def test_find_joins_no_stage_toolset():
    assert not kernel.stage_allows("contacts.find", stage="full")
    assert not kernel.stage_allows("contacts.find", stage="read_mostly")


@pytest.mark.pg
def test_upsert_and_find_round_trip(monkeypatch):
    from tests.test_store_promises import _ensure_test_db, _test_dsn
    from kyraan.store import pg
    if not pg.available():
        pytest.skip("local Postgres container unreachable")
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    from kyraan.store import contacts as cstore
    with pg.connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS contact (
            resource text PRIMARY KEY, name text NOT NULL,
            phones text[] NOT NULL DEFAULT '{}',
            emails text[] NOT NULL DEFAULT '{}',
            updated_at timestamptz NOT NULL DEFAULT now())""")
        conn.execute("DELETE FROM contact")
        conn.commit()
    n = cstore.upsert_all([
        {"resource": "people/1", "name": "Suman Ghosh",
         "phones": ["+91 1"], "emails": []},
        {"resource": "people/1", "name": "Suman Ghosh",
         "phones": ["+91 2"], "emails": ["s@x.com"]}])   # upsert wins
    assert n == 2
    hits = cstore.find("ghosh suman")
    assert hits == [{"name": "Suman Ghosh", "phones": ["+91 2"],
                     "emails": ["s@x.com"]}]
    pg.reset_pool_for_tests()
