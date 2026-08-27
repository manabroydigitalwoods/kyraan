"""P3.2a — fact sync. Subject derivation is pure-unit; the mirror tests
run against the real container (pg marker, MIRROR_ENABLED re-enabled on
top of the conftest default-off) and delete their own rows."""
import os
from pathlib import Path

import pytest

from kyraan.store.facts import OWNER, fact_uuid, subject_for

# --- subject derivation (audit P1) — no Postgres needed ------------------


def test_people_target_with_person_row_is_that_subject():
    entry = {"target": "people/kiaan.md"}
    assert subject_for(entry, {"kiaan"}) == ("kiaan", True)


def test_people_owner_file_is_the_owner():
    assert subject_for({"target": "people/owner.md"}, set()) == (OWNER, True)


def test_people_without_person_row_is_unresolved():
    # The mis-owning case the audit called out: a family fact must land
    # flagged for review, not silently owned.
    assert subject_for({"target": "people/father.md"}, set()) == (OWNER, False)


def test_non_people_categories_are_owner_facts():
    for target in ("routines/smoking.md", "preferences/tea.md", "work/dw.md"):
        assert subject_for({"target": target}, set()) == (OWNER, True)


def test_fact_uuid_is_deterministic():
    assert fact_uuid("abc12345") == fact_uuid("abc12345")
    assert fact_uuid("abc12345") != fact_uuid("abc12346")


# --- mirror integration (pg marker) --------------------------------------

_env = Path(__file__).resolve().parents[1] / ".env"
if "KYRAAN_PG_DSN" not in os.environ and _env.exists():
    for line in _env.read_text().splitlines():
        if line.startswith("KYRAAN_PG_DSN="):
            os.environ["KYRAAN_PG_DSN"] = line.split("=", 1)[1].strip()
            break

from kyraan.store import pg  # noqa: E402

_PG_UP = pg.available()


@pytest.fixture
def live_mirror(monkeypatch):
    """Re-enable mirroring for this test and delete every row it wrote."""
    from kyraan.store import facts
    monkeypatch.setattr(facts, "MIRROR_ENABLED", True)
    monkeypatch.setattr(facts, "_breaker_until", 0.0)
    from kyraan.memory import engine
    before = {e["id"] for e in engine._load()}
    yield
    created = [e["id"] for e in engine._load() if e["id"] not in before]
    if created:
        with pg.connection() as conn:
            conn.execute("DELETE FROM fact WHERE legacy_id = ANY(%s)", (created,))
            conn.commit()


def _row(legacy_id):
    with pg.connection() as conn:
        return conn.execute(
            """SELECT f.content, f.active, f.subject, f.subject_reviewed,
                      s.legacy_id
               FROM fact f LEFT JOIN fact s ON s.id = f.superseded_by
               WHERE f.legacy_id = %s""", (legacy_id,)).fetchone()


@pytest.mark.pg
@pytest.mark.skipif(not _PG_UP, reason="local Postgres container unreachable")
def test_promote_mirrors_a_row(live_mirror):
    from kyraan.memory import engine
    fid = engine.add_fact("Drinks tea at dawn (sync test)",
                          "preferences/tea.md", "test")
    row = _row(fid)
    assert row is not None
    content, active, subject, reviewed, _ = row
    assert content == "Drinks tea at dawn (sync test)"
    assert active and subject == OWNER and reviewed


@pytest.mark.pg
@pytest.mark.skipif(not _PG_UP, reason="local Postgres container unreachable")
def test_forget_mirrors_active_false(live_mirror):
    from kyraan.memory import engine
    fid = engine.add_fact("Temporary sync fact", "preferences/tmp.md", "test")
    engine.forget([fid])
    assert _row(fid)[1] is False


@pytest.mark.pg
@pytest.mark.skipif(not _PG_UP, reason="local Postgres container unreachable")
def test_supersede_mirrors_the_link(live_mirror):
    from kyraan.memory import engine
    old = engine.add_fact("Favourite sync snack is idli",
                          "preferences/snack.md", "test")
    new = engine.add_fact("Favourite sync snack is dosa",
                          "preferences/snack.md", "test",
                          supersedes="Favourite sync snack is idli")
    old_row = _row(old)
    assert old_row[1] is False          # deactivated
    assert old_row[4] == new            # superseded_by → the new fact
    assert _row(new)[1] is True


@pytest.mark.pg
@pytest.mark.skipif(not _PG_UP, reason="local Postgres container unreachable")
def test_unresolved_people_fact_lands_flagged(live_mirror):
    from kyraan.memory import engine
    fid = engine.add_fact("Cousin Bikram lives in Pune (sync test)",
                          "people/bikram.md", "test")
    _, _, subject, reviewed, _ = _row(fid)
    assert subject == OWNER and reviewed is False


def test_pg_down_file_op_succeeds_and_defers(monkeypatch):
    from kyraan.memory import engine
    from kyraan.store import facts
    monkeypatch.setattr(facts, "MIRROR_ENABLED", True)
    monkeypatch.setattr(facts, "_breaker_until", 0.0)

    def boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(facts.pg, "connection", boom)
    events = []
    monkeypatch.setattr(facts, "log_event",
                        lambda name, **kw: events.append(name))
    fid = engine.add_fact("Survives PG outage", "preferences/x.md", "test")
    assert fid  # the file write stands
    assert any(e["id"] == fid for e in engine._load())
    assert "fact_sync_deferred" in events
    # and the breaker is now open: the next mirror skips the connection
    events.clear()
    engine.add_fact("Second while down", "preferences/y.md", "test")
    assert "fact_sync_deferred" in events
