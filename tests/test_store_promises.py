"""P3.2d — promises to Postgres. Every mutation runs parametrized over
both backends; the lease/crash-window semantics are proven on pg. The pg
legs use a dedicated `kyraan_test` DATABASE (created on demand) so the
full-state mirror can never touch the live tables."""
import json
import os
from pathlib import Path

import pytest

_env = Path(__file__).resolve().parents[1] / ".env"
if "KYRAAN_PG_DSN" not in os.environ and _env.exists():
    for line in _env.read_text().splitlines():
        if line.startswith("KYRAAN_PG_DSN="):
            os.environ["KYRAAN_PG_DSN"] = line.split("=", 1)[1].strip()
            break

from kyraan.store import pg, promises  # noqa: E402
from kyraan.triggers import agent_tasks, store  # noqa: E402

_PG_UP = pg.available()
_REPO = Path(__file__).resolve().parents[1]


def _test_dsn() -> str:
    base = pg.dsn()
    return base.rsplit("/", 1)[0] + "/kyraan_test"


def _ensure_test_db() -> None:
    import psycopg
    with psycopg.connect(pg.dsn(), autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = 'kyraan_test'").fetchone():
            conn.execute("CREATE DATABASE kyraan_test")
    with psycopg.connect(_test_dsn()) as conn:
        for path in sorted((_REPO / "migrations").glob("*.sql")):
            conn.execute(path.read_text())  # all migrations are idempotent
        conn.commit()


def _enter_pg(monkeypatch) -> None:
    if not _PG_UP:
        pytest.skip("local Postgres container unreachable")
    _ensure_test_db()
    monkeypatch.setenv("KYRAAN_PG_DSN", _test_dsn())
    pg.reset_pool_for_tests()
    monkeypatch.setattr(promises, "MIRROR_ENABLED", True)
    monkeypatch.setattr(promises, "_breaker_until", 0.0)
    monkeypatch.setenv("KYRAAN_PROMISES_BACKEND", "pg")
    with pg.connection() as conn:
        conn.execute("TRUNCATE reminder, agent_task, cost_ledger")
        conn.commit()


@pytest.fixture(params=["files", pytest.param("pg", marks=pytest.mark.pg)])
def backend(request, monkeypatch):
    """files: flag unset, mirroring off (conftest default). pg: mirroring
    on into kyraan_test, reads flagged to pg."""
    if request.param == "pg":
        _enter_pg(monkeypatch)
        yield "pg"
        pg.reset_pool_for_tests()  # next test rebuilds on the real DSN
    else:
        # post-cutover the code default is pg — files needs the explicit
        # rollback lever
        monkeypatch.setenv("KYRAAN_PROMISES_BACKEND", "files")
        yield "files"


@pytest.fixture
def pg_backend(monkeypatch):
    """The pg leg alone — for the lease/cutover tests."""
    _enter_pg(monkeypatch)
    yield
    pg.reset_pool_for_tests()


# --- every mutation, both backends ----------------------------------------

def test_add_and_list_pending(backend):
    r = store.add(11, "water plants", "2026-08-28T08:30:00+05:30")
    pending = store.list_pending(11)
    assert [p.id for p in pending] == [r.id]
    assert pending[0].text == "water plants"
    assert store.get(r.id).when_iso == "2026-08-28T08:30:00+05:30"


def test_mark_sent_retires(backend):
    r = store.add(11, "one shot", "2026-08-28T08:30:00+05:30")
    store.mark_sent(r.id)
    assert store.list_pending(11) == []


def test_cancel_removes(backend):
    r = store.add(11, "to cancel", "2026-08-28T08:30:00+05:30")
    assert store.cancel(r.id) is True
    assert store.get(r.id) is None


def test_claim_and_release_visible(backend):
    r = store.add(11, "claimed", "2026-08-28T08:30:00+05:30")
    assert store.claim_for_send(r.id) is True
    assert store.claim_for_send(r.id) is False  # live lease
    assert store.get(r.id).claimed_at != ""
    store.release_claim(r.id)
    assert store.get(r.id).claimed_at == ""


def test_roll_forward_advances_and_clears(backend):
    r = store.add(11, "daily", "2026-08-28T08:30:00+05:30", repeat="daily")
    store.claim_for_send(r.id)
    store.roll_forward(r.id, "2026-08-29T08:30:00+05:30")
    rolled = store.get(r.id)
    assert rolled.when_iso == "2026-08-29T08:30:00+05:30"
    assert rolled.claimed_at == "" and rolled.takeover is False
    assert not rolled.sent  # a series never retires by delivery


def test_task_create_cancel_and_pending_result(backend, monkeypatch):
    monkeypatch.setattr(agent_tasks, "_schedule_fn", lambda *a, **k: None)
    t = agent_tasks.create(11, "check calendar and report", "2026-08-28T20:00:00+05:30")
    assert [x.id for x in agent_tasks.list_active(11)] == [t.id]
    agent_tasks._set_pending_result(t.id, "produced but undelivered")
    assert agent_tasks.list_active(11)[0].pending_result == "produced but undelivered"
    agent_tasks.cancel(t.id)
    assert agent_tasks.list_active(11) == []


def test_ledger_spend_readable(backend):
    from kyraan.model_router import router
    router._record_cost(0.5)
    router._record_cost(0.25)
    assert router.today_cost_usd() == 0.75


# --- crash-window semantics on pg (the cutover's load-bearing proof) ------

@pytest.mark.pg
def test_pg_claim_lease_semantics(pg_backend):
    from datetime import datetime, timedelta, timezone
    r = store.add(11, "lease test", "2026-08-28T08:30:00+05:30")
    assert promises.pg_claim_for_send(r.id) is True
    assert promises.pg_claim_for_send(r.id) is False  # unexpired lease
    # a crashed sender: age the claim past the lease directly in pg
    stale = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    with pg.connection() as conn:
        conn.execute("UPDATE reminder SET claimed_at = %s WHERE id = %s",
                     (stale, r.id))
        conn.commit()
    assert promises.pg_claim_for_send(r.id) is True  # takeover
    with pg.connection() as conn:
        takeover, = conn.execute(
            "SELECT takeover FROM reminder WHERE id = %s", (r.id,)).fetchone()
    assert takeover is True  # sticky: the send must say "may be a repeat"
    # sent ends all claiming
    store.mark_sent(r.id)
    assert promises.pg_claim_for_send(r.id) is False


@pytest.mark.pg
def test_restart_with_pg_only_serves_pending(pg_backend, monkeypatch, tmp_path):
    """The Done-when: the file is GONE after a 'restart'; flag=pg still
    serves every pending reminder field-for-field."""
    a = store.add(11, "morning walk", "2026-08-28T06:00:00+05:30", repeat="daily")
    b = store.add(12, "one shot", "2026-08-28T09:00:00+05:30")
    sent = store.add(11, "already sent", "2026-08-27T09:00:00+05:30")
    store.mark_sent(sent.id)
    monkeypatch.setattr(store, "REMINDERS_PATH", tmp_path / "gone.json")
    pending = {r.id: r for r in store.list_pending()}
    assert set(pending) == {a.id, b.id}
    assert pending[a.id].repeat == "daily"
    assert pending[a.id].when_iso == "2026-08-28T06:00:00+05:30"


@pytest.mark.pg
def test_pg_backend_falls_back_to_files_on_error(pg_backend, monkeypatch):
    r = store.add(11, "survives fallback", "2026-08-28T08:30:00+05:30")
    events = []
    monkeypatch.setattr(promises, "log_event",
                        lambda name, **kw: events.append(name))

    def boom():
        raise RuntimeError("pool down")

    monkeypatch.setattr(promises.pg, "connection", boom)
    assert [p.id for p in store.list_pending(11)] == [r.id]  # file served
    assert "promises_backend_fallback" in events


def test_mirror_failure_never_blocks_the_file_write(monkeypatch):
    monkeypatch.setattr(promises, "MIRROR_ENABLED", True)
    monkeypatch.setattr(promises, "_breaker_until", 0.0)
    events = []
    monkeypatch.setattr(promises, "log_event",
                        lambda name, **kw: events.append(name))

    def boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(promises.pg, "connection", boom)
    r = store.add(11, "file stands", "2026-08-28T08:30:00+05:30")
    assert json.loads(store.REMINDERS_PATH.read_text())[0]["id"] == r.id
    assert "promise_sync_deferred" in events
