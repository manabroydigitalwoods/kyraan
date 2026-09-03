"""Whereabouts — location awareness (2026-09-04)."""
import asyncio

from kyraan.triggers import whereabouts as wh

HOME = (26.6536, 88.4724)


def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(wh, "STATE_PATH", tmp_path / "w.json")
    monkeypatch.setattr(wh, "home", lambda: HOME)


def test_homeward_fires_once_per_trip_when_closing_in(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    far = (26.72, 88.47)                     # ~7.4 km north
    assert wh.observe(*far, when=1) == []
    mid = (26.675, 88.4724)                  # ~2.4 km, closing in
    got = wh.observe(*mid, when=2)
    assert got and got[0][0] == "homeward" and "minutes from home" in got[0][1]
    assert wh.observe(26.665, 88.4724, when=3) == []             # not again this trip
    assert wh.observe(*far, when=4) == []                        # leaving re-arms
    assert wh.observe(*mid, when=5)[0][0] == "homeward"


def test_named_place_arrival_once_per_visit(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    monkeypatch.setattr(wh.time, "time", lambda: 1000.0)
    wh.observe(26.70, 88.50, when=990)
    assert wh.remember_place("clinic")["lat"] == 26.70
    wh.observe(26.60, 88.40, when=995)                            # away
    got = wh.observe(26.7001, 88.5001, when=999)                  # back at the clinic
    assert got == [("arrived", "clinic")]
    assert wh.observe(26.7002, 88.5001, when=1000) == []          # still there: silent
    assert wh.forget_place("clinic") and wh.places() == {}


def test_where_text_and_rails(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    monkeypatch.setattr(wh, "where_text", lambda: "Last I knew (3 min ago): Sevoke Road, Siliguri, 2.1 km from home.")
    assert asyncio.run(orchestrator.handle_message(1, "where am I?")).startswith("Last I knew")
    monkeypatch.setattr(wh, "last_fix", lambda: None)
    out = asyncio.run(orchestrator.handle_message(1, "remember this place as the clinic"))
    assert out.startswith("I need a recent location first")


def test_homeward_becomes_an_ac_ask_when_the_ac_is_off(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    from kyraan.agents import orchestrator
    monkeypatch.setattr(wh.kernel, "can_send_proactively", lambda **kw: True)

    async def fake_run(call, **kw): return {"entity": "switch.ac", "state": "off"}
    monkeypatch.setattr(wh.kernel, "run_tool", fake_run)

    async def fake_gated(chat_id, call, handler, describe="", **kw):
        return describe + ' — reply "yes" to confirm or "no" to cancel.'
    monkeypatch.setattr(orchestrator, "_gated", fake_gated)
    sent = []

    async def send(chat_id, text): sent.append(text); return True
    n = asyncio.run(wh.announce(1, [("homeward", "You're about 6 minutes from home (2.4 km).")], send))
    assert n == 1 and "Turn the AC on" in sent[0] and 'reply "yes"' in sent[0]


def test_where_phrasings_and_coarse_fix_rejected(monkeypatch, tmp_path):
    import asyncio
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    from kyraan.tools import home_assistant as ha
    _iso(monkeypatch, tmp_path)
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    monkeypatch.setattr(wh, "where_text", lambda: "Last I knew (0 min ago): home.")
    for q in ("where i am?", "where am I now", "my location", "can you check again?", "did you get live location?"):
        assert asyncio.run(orchestrator.handle_message(1, q)).startswith("Last I knew"), q
    wh._person_seen.clear()
    monkeypatch.setattr(ha, "_raw", lambda entity: {"attributes": {"latitude": 26.71, "longitude": 88.42, "gps_accuracy": 3200}, "last_updated": "t9"})

    async def send(c, t): return True
    assert asyncio.run(wh.poll_person(1, send)) == 0 and wh.last_fix() is None      # coarse: ignored


def test_keeper_takes_any_kiaan_vaccination_ask(monkeypatch):
    import asyncio
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    from kyraan.triggers import kiaan_keeper
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    monkeypatch.setattr(kiaan_keeper, "status_text", lambda: "Kiaan is 10 months old.")
    assert asyncio.run(orchestrator.handle_message(1, "kiaan’s vaccination upcoming days")).startswith("Kiaan is 10")


def test_phone_status_rail(monkeypatch, tmp_path):
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    from kyraan.tools import home_assistant as ha
    _iso(monkeypatch, tmp_path)
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    raw = {"device_tracker.manabs_iphone": {"state": "home", "attributes": {"gps_accuracy": 8.1}, "last_updated": "2026-09-04T00:00:00+00:00"},
           "sensor.manabs_iphone_battery_level": {"state": "80"},
           "sensor.manabs_iphone_battery_state": {"state": "Not Charging", "attributes": {"Low Power Mode": False}},
           "sensor.manabs_iphone_location_permission": {"state": "Authorized Always"},
           "sensor.manabs_iphone_app_version": {"state": "2026.9.0"}}
    monkeypatch.setattr(ha, "_raw", lambda e: raw[e])
    for q in ("what you can tell me about this phone?", "about my phone", "phone battery"):
        out = asyncio.run(orchestrator.handle_message(1, q))
        assert "Battery 80%, not charging" in out and "at home (GPS accuracy 8 m)" in out and "Authorized Always" in out, q


def test_forget_narrows_by_the_owners_words(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.memory import engine
    from kyraan.control_plane import kernel
    facts = [{"id": "a", "content": "Every day remind me every 5 minutes to drink water."},
             {"id": "b", "content": "User wants reminders every hour each day to drink water."}]
    monkeypatch.setattr(engine, "find_matches", lambda t: list(facts))
    monkeypatch.setattr(kernel, "confirmed_context", lambda: True)
    got = []
    monkeypatch.setattr(engine, "forget", lambda ids: got.extend(ids) or ["x"])
    asyncio.run(loop_tools._memory_forget(1, {"fact": "5 minute water reminder"}, "forget the 5 minute water reminder fact"))
    assert got == ["a"]


def test_forget_confirm_ask_lists_only_the_narrowed_fact(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.memory import engine
    facts = [{"id": "a", "content": "Every day remind me every 5 minutes to drink water."},
             {"id": "b", "content": "User wants reminders every hour each day to drink water."}]
    monkeypatch.setattr(engine, "find_matches", lambda t: list(facts))
    text = loop_tools._describe_call("memory.forget", {"fact": "5 minute water reminder"})
    assert "5 minutes" in text and "every hour" not in text
