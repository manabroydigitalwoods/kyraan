"""Gap fills 2026-09-04: numbered answers, duties status, phone tracker."""
import asyncio


def test_a_bare_number_answers_an_enumerated_list(monkeypatch):
    from kyraan.agents import orchestrator, session, agent_loop
    from kyraan.control_plane import kernel
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "memory")
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    monkeypatch.setattr(orchestrator, "log_chat", lambda *a, **k: None)
    reached = []

    async def fake_run(chat_id, raw_text, tier="frontier", read_only=False):
        reached.append(raw_text); return "picked"
    monkeypatch.setattr(agent_loop, "run", fake_run)
    session._history[71] = [("user", "which playlist"), ("assistant", "Options:\n1) Hindi rhymes\n2) English kids\n3) Bedtime")]
    assert asyncio.run(orchestrator.handle_message(71, "3")) == "picked" and reached == ["3"]
    session._history[72] = [("user", "hi"), ("assistant", "Hello Maan.")]
    assert asyncio.run(orchestrator.handle_message(72, "3")).startswith("Go on")      # still a fragment
    session._history[71] = []; session._history[72] = []


def test_duties_status_lists_each_duty(monkeypatch, tmp_path):
    from kyraan.triggers import duties, kiaan_keeper, chief_of_staff, house_steward, whereabouts
    for mod in (kiaan_keeper, chief_of_staff, house_steward, whereabouts):
        monkeypatch.setattr(mod, "STATE_PATH", tmp_path / f"{mod.__name__}.json")
    out = duties.status_text()
    for name in ("Kiaan's keeper", "Chief of staff", "House steward", "Whereabouts", "Voice"):
        assert name in out


def test_person_tracker_feeds_whereabouts(monkeypatch, tmp_path):
    from kyraan.triggers import whereabouts as wh
    from kyraan.tools import home_assistant as ha
    monkeypatch.setattr(wh, "STATE_PATH", tmp_path / "w.json")
    monkeypatch.setattr(wh, "home", lambda: (26.6536, 88.4724))
    wh._person_seen.clear()
    states = [{"attributes": {}, "last_updated": "t0"},                                        # no tracker yet
              {"attributes": {"latitude": 26.72, "longitude": 88.47}, "last_updated": "t1"},
              {"attributes": {"latitude": 26.72, "longitude": 88.47}, "last_updated": "t1"},   # unchanged
              {"attributes": {"latitude": 26.675, "longitude": 88.4724}, "last_updated": "t2"}]
    monkeypatch.setattr(ha, "_raw", lambda entity: states.pop(0))
    monkeypatch.setattr(wh.kernel, "can_send_proactively", lambda **kw: True)

    async def fake_run(call, **kw): return {"state": "on"}
    monkeypatch.setattr(wh.kernel, "run_tool", fake_run)
    sent = []

    async def send(c, t): sent.append(t); return True
    assert asyncio.run(wh.poll_person(1, send)) == 0          # unknown position
    assert asyncio.run(wh.poll_person(1, send)) == 0          # far out, nothing to say
    assert asyncio.run(wh.poll_person(1, send)) == 0          # same stamp, ignored
    assert asyncio.run(wh.poll_person(1, send)) == 1          # closing in: homeward line
    assert "minutes from home" in sent[0]
