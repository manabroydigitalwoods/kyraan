"""P3.4b — the confirmation stash survives a restart: ask → new process
→ yes executes the stashed tool call byte-identically. Plus nonce
integrity across the restart, TTL expiry, the closure-ask exclusion,
and P3.4c's proof that a Redis FLUSH loses no spend accounting."""
import time

import pytest

from kyraan.agents import agent_loop, orchestrator
from kyraan.control_plane import kernel
from kyraan.store import redis_kv
from tests.test_session_backend import _REDIS_UP

pg_only = pytest.mark.skipif(not _REDIS_UP, reason="local Redis container unreachable")


@pytest.fixture
def redis_confirms(monkeypatch):
    if not _REDIS_UP:
        pytest.skip("local Redis container unreachable")
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "redis")
    monkeypatch.setenv("KYRAAN_REDIS_URL", "redis://127.0.0.1:6379/9")
    redis_kv.reset_for_tests()
    redis_kv.client().flushdb()
    orchestrator._pending_confirmations.clear()
    orchestrator._confirmation_nonce.clear()
    yield
    orchestrator._pending_confirmations.clear()
    orchestrator._confirmation_nonce.clear()
    redis_kv.reset_for_tests()


async def _ask(chat_id, tool="reminders.create",
               args=None, raw_text="remind me to call mom at 9pm"):
    """Drive the loop's confirm path exactly as agent_loop does."""
    args = args if args is not None else {"text": "call mom",
                                          "when_iso": "2027-01-01T21:00:00+05:30"}

    async def gate(_a):
        raise kernel.ConfirmationRequired(tool, args)

    return await orchestrator._gated(
        chat_id, kernel.SkillCall("agent.action", {"tool": tool}), gate,
        describe="About to set the reminder",
        replay={"tool": tool, "args": args, "raw_text": raw_text})


def _restart():
    """Everything a process restart forgets."""
    orchestrator._pending_confirmations.clear()
    orchestrator._confirmation_nonce.clear()
    redis_kv.reset_for_tests()


@pytest.mark.pg
async def test_yes_after_restart_executes_byte_identically(redis_confirms, monkeypatch):
    chat_id = 930_001
    ask = await _ask(chat_id)
    assert 'reply "yes"' in ask
    executed = []

    async def fake_run(cid, args, raw_text):
        executed.append((cid, dict(args), raw_text))
        return {"created": True, "id": "r1", "text": args["text"], "when": "9pm"}

    monkeypatch.setitem(agent_loop.TOOLS["reminders.create"], "run", fake_run)
    from kyraan.store import actions
    monkeypatch.setattr(actions, "record", lambda *a, **k: "aid")
    _restart()
    assert orchestrator._pending_confirmations == {}  # really gone in-proc
    reply = await orchestrator._dispatch(chat_id, "yes")
    assert executed == [(chat_id,
                         {"text": "call mom", "when_iso": "2027-01-01T21:00:00+05:30"},
                         "remind me to call mom at 9pm")]
    assert "call mom" in reply
    # resolved: the survivor must not fire twice
    assert redis_kv.get_json(redis_kv.key("confirm", chat_id)) is None


@pytest.mark.pg
async def test_nonce_survives_the_restart(redis_confirms):
    chat_id = 930_002
    await _ask(chat_id)
    nonce_before = orchestrator._confirmation_nonce[chat_id]
    _restart()
    assert orchestrator.current_confirmation_nonce(chat_id) == nonce_before


@pytest.mark.pg
async def test_no_after_restart_cancels_and_clears(redis_confirms, monkeypatch):
    chat_id = 930_003
    await _ask(chat_id)
    executed = []
    monkeypatch.setitem(agent_loop.TOOLS["reminders.create"], "run",
                        lambda *a, **k: executed.append(a))
    _restart()
    reply = await orchestrator._dispatch(chat_id, "no")
    assert "nothing was done" in reply.lower()
    assert executed == []
    assert redis_kv.get_json(redis_kv.key("confirm", chat_id)) is None


@pytest.mark.pg
async def test_expired_survivor_is_gone(redis_confirms, monkeypatch):
    chat_id = 930_004
    monkeypatch.setattr(orchestrator, "_CONFIRMATION_TTL_S", 1)
    await _ask(chat_id)
    _restart()
    time.sleep(1.2)  # Redis TTL is the expiry rule for survivors
    assert orchestrator._load_persisted_confirmation(chat_id) is None


@pytest.mark.pg
async def test_closure_asks_do_not_persist(redis_confirms):
    chat_id = 930_005

    async def gate(_a):
        raise kernel.ConfirmationRequired("faces.forget", {"name": "x"})

    await orchestrator._gated(
        chat_id, kernel.SkillCall("faces.forget", {"name": "x"}), gate,
        describe="About to DELETE the stored face")  # no replay:
    assert redis_kv.get_json(redis_kv.key("confirm", chat_id)) is None
    assert chat_id in orchestrator._pending_confirmations  # in-proc only


# --- P3.4c: a Redis flush loses no spend ----------------------------------

@pytest.mark.pg
def test_flushall_and_restart_still_know_todays_spend(redis_confirms):
    from kyraan.model_router import router
    router._record_cost(0.42)
    redis_kv.client().flushall()
    _restart()
    assert router.today_cost_usd() == 0.42  # the ledger never lived in Redis