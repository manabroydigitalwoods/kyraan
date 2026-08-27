"""P3.4a — session state over both backends: the same API parity suite
runs against memory and Redis (test DB 9, flushed per test; skipped when
the container is down). Plus the two contract tests: restart survival
on redis, and redis-down degrading to exactly today's behavior."""
import pytest

from kyraan.agents import session
from kyraan.store import redis_kv


def _redis_up() -> bool:
    try:
        import redis
        redis.Redis.from_url("redis://127.0.0.1:6379/9",
                             socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


_REDIS_UP = _redis_up()


def _fresh_session(monkeypatch):
    monkeypatch.setattr(session, "_history", session._PerChat(session._ChatHistory))
    monkeypatch.setattr(session, "_summary_backlog",
                        session._PerChat(session._ChatBacklog))


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.pg)])
def backend(request, monkeypatch):
    _fresh_session(monkeypatch)
    if request.param == "redis":
        if not _REDIS_UP:
            pytest.skip("local Redis container unreachable")
        monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "redis")
        monkeypatch.setenv("KYRAAN_REDIS_URL", "redis://127.0.0.1:6379/9")
        redis_kv.reset_for_tests()
        redis_kv.client().flushdb()
        yield "redis"
        redis_kv.reset_for_tests()
    else:
        monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "memory")
        redis_kv.reset_for_tests()
        yield "memory"


def test_record_and_read_back(backend):
    session.record_exchange(7, "hello", "hi there")
    assert list(session._history[7]) == [("user", "hello"), ("assistant", "hi there")]
    assert "user: hello" in session._history_block(7)


def test_window_rolls_into_backlog(backend):
    for i in range(21):  # 21 exchanges = 42 entries > the 40-entry window
        session.record_exchange(7, f"q{i}", f"a{i}")
    assert len(session._history[7]) == session._HISTORY_MAX_ENTRIES
    assert list(session._history[7])[0] == ("user", "q1")  # q0 rolled out
    assert list(session._summary_backlog[7]) == [("user", "q0"), ("assistant", "a0")]


def test_proactive_lands_in_history(backend):
    session.record_proactive(7, "⏰ water")
    assert list(session._history[7]) == [("assistant", "⏰ water")]


def test_backlog_reassignment_and_radd(backend):
    session._summary_backlog[7].append(("user", "old"))
    backlog = session._summary_backlog.get(7) or []
    assert backlog == [("user", "old")]
    session._summary_backlog[7] = backlog[1:]           # the roll's slice-assign
    assert len(session._summary_backlog[7]) == 0
    session._summary_backlog[7] = [("user", "x")] + session._summary_backlog[7]
    assert list(session._summary_backlog[7]) == [("user", "x")]


def test_seed_never_clobbers_live_history(backend, monkeypatch, tmp_path):
    session.record_exchange(7, "live", "conversation")
    log = tmp_path / "chat.jsonl"
    log.write_text('{"ts":"2026-08-27T05:00:00+00:00","chat_id":7,'
                   '"role":"user","text":"from the log"}\n')
    from kyraan.control_plane import logging_setup
    monkeypatch.setattr(logging_setup, "CHAT_LOG", log)
    session.seed_history_from_log()
    assert ("user", "from the log") not in list(session._history[7])


# --- the two contract tests -----------------------------------------------

@pytest.mark.pg
def test_restart_survival_on_redis(monkeypatch):
    """The Done-when: a 'restart' (fresh proxies, fresh client — all
    process state gone) still sees the conversation."""
    if not _REDIS_UP:
        pytest.skip("local Redis container unreachable")
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "redis")
    monkeypatch.setenv("KYRAAN_REDIS_URL", "redis://127.0.0.1:6379/9")
    _fresh_session(monkeypatch)
    redis_kv.reset_for_tests()
    redis_kv.client().flushdb()
    session.record_exchange(7, "are those the latest emails?", "yes — 5 unread")
    # the restart: every in-process structure is rebuilt from nothing
    _fresh_session(monkeypatch)
    redis_kv.reset_for_tests()
    assert list(session._history[7]) == [
        ("user", "are those the latest emails?"),
        ("assistant", "yes — 5 unread")]
    redis_kv.reset_for_tests()


def test_redis_down_degrades_to_memory_with_one_event(monkeypatch):
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "redis")
    monkeypatch.setenv("KYRAAN_REDIS_URL", "redis://127.0.0.1:1/0")  # nothing there
    _fresh_session(monkeypatch)
    redis_kv.reset_for_tests()
    events = []
    monkeypatch.setattr(redis_kv, "log_event",
                        lambda name, **kw: events.append(name))
    session.record_exchange(7, "hello", "hi")   # falls back silently
    session.record_exchange(7, "more", "text")
    assert list(session._history[7]) == [("user", "hello"), ("assistant", "hi"),
                                         ("user", "more"), ("assistant", "text")]
    assert events.count("session_backend_fallback") == 1  # once, not per op
    redis_kv.reset_for_tests()
