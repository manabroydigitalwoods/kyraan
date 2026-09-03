"""Voice in the room — duty #4 (2026-09-03)."""
import asyncio

from kyraan.channels import voice_echo as ve


def test_wake_word_and_variants():
    assert ve.parse_wake("kyraan what's open") == "what's open"
    assert ve.parse_wake("Kiran, when is Kiaan's next vaccine") == "when is Kiaan's next vaccine"
    assert ve.parse_wake("hey kyraan turn on the ac") == "turn on the ac"
    assert ve.parse_wake("current house status") == "house status"      # Alexa's spelling of Kyraan, live
    assert ve.parse_wake("ask kyraan what's open") == "what's open"
    assert ve.parse_wake("house status") == "house status"                # Kyraan's own commands need no name
    assert ve.parse_wake("what's open?") == "what's open"
    assert ve.parse_wake("kiaan status") == "kiaan status"
    assert ve.parse_wake("play some music") is None                        # Alexa's own business
    assert ve.parse_wake("set a timer for 10 minutes") is None
    assert ve.parse_wake("stop") is None
    assert ve.parse_wake("kyraan") is None                       # the name alone is not a request


def test_speech_is_plain_and_short():
    reply = ("Open right now:\n- Slack #dev: kamal — \"eta?\"\n- Slipped: call mom (was 5:00 PM)"
             "\n\n☁️ via cloud")
    s = ve.for_speech(reply)
    assert "\n" not in s and "☁" not in s and "Slack #dev" in s
    long = "Sentence one is here. " * 30
    s = ve.for_speech(long)
    assert len(s) <= ve.SPEECH_MAX + 30 and s.endswith("The rest is on Telegram.")


def test_tick_handles_new_utterances_only(monkeypatch):
    monkeypatch.setattr(ve, "enabled", lambda: True)
    monkeypatch.setattr(ve, "devices", lambda: ["media_player.manab_s_echo_dot"])
    ve._last_ts.clear()
    now_ms = int(ve.time.time() * 1000)
    state = {"ts": now_ms, "summary": "kyraan what's open"}

    async def read(entity): return state["ts"], state["summary"]
    monkeypatch.setattr(ve, "read_last_called", read)
    handled = []

    async def fake_handle(chat, entity, text, send): handled.append(text)
    monkeypatch.setattr(ve, "handle", fake_handle)

    async def send(c, t): return True
    assert asyncio.run(ve.tick(1, send)) == 0 and handled == []            # first sight: never replay
    state.update(ts=now_ms + 1000, summary="kyraan house status")
    assert asyncio.run(ve.tick(1, send)) == 1 and handled == ["house status"]
    assert asyncio.run(ve.tick(1, send)) == 0                               # same utterance again: ignored
    state.update(ts=now_ms + 2000, summary="play some music")
    assert asyncio.run(ve.tick(1, send)) == 0 and handled == ["house status"]   # not for us


def test_handle_speaks_and_mirrors(monkeypatch):
    from kyraan.agents import orchestrator
    spoken, mirrored = [], []

    async def fake_msg(chat, text): return "Nothing open — all done.\n\n🔒 on this Mac"
    monkeypatch.setattr(orchestrator, "handle_message", fake_msg)

    async def fake_run(call, **kw):
        spoken.append(call.args); return {"ok": True}
    monkeypatch.setattr(ve.kernel, "run_tool", fake_run)

    async def send(c, t): mirrored.append(t); return True
    asyncio.run(ve.handle(1, "media_player.manab_s_echo_dot", "what's open", send))
    assert spoken[0]["target"] == "manab_s_echo_dot" and spoken[0]["message"] == "Nothing open — all done."
    assert mirrored[0].startswith("🎙 You said: what's open")
