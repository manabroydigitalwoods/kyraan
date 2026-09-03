"""Secrets (owner 2026-09-03): a secret is handled end to end on this
machine and leaves only a placeholder anywhere a cloud prompt could read."""
import asyncio
import json

import pytest

from kyraan.agents import secrets


def test_secret_phrases_in_any_wording():
    assert secrets.opens("ek secret baat hai or esko secret rakha hai ok?")
    assert secrets.opens("keep this between us")
    assert secrets.opens("this is confidential, don't tell anyone")
    assert not secrets.opens("what is the secret ingredient of biryani")   # a bare noun never opens
    assert not secrets.opens("what did we discuss this morning")
    assert secrets.retro("isko secret rakho")
    assert secrets.retro("keep this secret")
    assert not secrets.retro("ek secret baat hai")            # announcing, not retro
    assert secrets.closes("bas itna hi") and secrets.closes("that's all") and not secrets.closes("bas itna hi aur ek baat")


def test_window_opens_extends_and_closes(monkeypatch):
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "memory")
    secrets.close(99)
    assert not secrets.active(99)
    secrets.touch(99)
    assert secrets.active(99)
    secrets.close(99)
    assert not secrets.active(99)


def test_redact_recent_and_log_readers_agree(monkeypatch, tmp_path):
    from kyraan.agents import session
    from kyraan.control_plane import logging_setup
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "memory")
    log = tmp_path / "chat.jsonl"
    monkeypatch.setattr(logging_setup, "CHAT_LOG", log)
    monkeypatch.setattr(secrets, "log_chat",
                        lambda chat_id, role, text, **f: log.open("a").write(
                            json.dumps({"ts": "2026-09-03T00:00:00+00:00", "chat_id": chat_id, "role": role, "text": text, **f}) + "\n"))
    rows = [("user", "how is the weather"), ("assistant", "sunny"),
            ("user", "i have a girlfriend"), ("assistant", "ohh nice, secret rakhunga")]
    session._history[55] = rows
    with log.open("a") as fh:
        for r, t in rows:
            fh.write(json.dumps({"ts": "2026-09-02T20:00:00+00:00", "chat_id": 55, "role": r, "text": t}) + "\n")
    assert secrets.redact_recent(55, entries=2) == 2
    assert list(session._history[55])[2:] == [("user", secrets.PLACEHOLDER), ("assistant", secrets.PLACEHOLDER)]
    assert list(session._history[55])[:2] == rows[:2]
    # the log readers (restart seeding, episodes) see the same placeholders
    parsed = [json.loads(l) for l in log.read_text().splitlines()]
    cloud = [e.get("cloud_text") or e["text"] for e in secrets.apply_redactions(parsed) if e["role"] != "redact"]
    assert cloud == ["how is the weather", "sunny", secrets.PLACEHOLDER, secrets.PLACEHOLDER]
    session._history[55] = []
    session.seed_history_from_log()
    assert list(session._history[55])[2:] == [("user", secrets.PLACEHOLDER), ("assistant", secrets.PLACEHOLDER)]
    # contains-mode redacts every earlier mention
    session._history[55] = rows
    assert secrets.redact_recent(55, contains="girlfriend") == 1
    assert list(session._history[55])[2] == ("user", secrets.PLACEHOLDER)
    session._history[55] = []


def test_secret_turn_is_local_only_and_leaves_placeholders(monkeypatch):
    from kyraan.agents import orchestrator, session, agent_loop
    from kyraan.control_plane import kernel, logging_setup
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "memory")
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    seen = []
    async def fake_run(chat_id, raw_text, tier="frontier", read_only=False):
        seen.append((tier, False)); return "loop reply"
    monkeypatch.setattr(agent_loop, "run", fake_run)
    from kyraan.model_router import router
    def fake_call(**kw):
        seen.append((kw["tier"], True))
        assert "THINGS YOU CAN" not in kw["system"] and len(kw["prompt"]) < 2000   # lean, not the loop
        class R: text = "ok, secret rakha"
        return R()
    monkeypatch.setattr(router, "call", fake_call)
    logged = []
    monkeypatch.setattr(orchestrator, "log_chat", lambda chat_id, role, text, **f: logged.append((role, text, f.get("cloud_text"))))
    session._history[56] = []
    secrets.close(56)
    out = asyncio.run(orchestrator.handle_message(56, "ek secret baat hai, i have a girlfriend"))
    assert out.startswith("ok, secret rakha")
    assert seen == [("cheap", True)]                                # never the frontier
    assert list(session._history[56]) == [("user", secrets.PLACEHOLDER), ("assistant", secrets.PLACEHOLDER)]
    assert [(r, c) for r, _, c in logged] == [("user", secrets.PLACEHOLDER), ("assistant", secrets.PLACEHOLDER)]
    assert secrets.active(56)                                        # the window stays open
    asyncio.run(orchestrator.handle_message(56, "bas itna hi"))
    assert seen[-1] == ("cheap", True) and not secrets.active(56)    # closed, still local
    asyncio.run(orchestrator.handle_message(56, "what's the weather"))
    assert seen[-1][0] == "frontier"                                 # back to normal
    session._history[56] = []


def test_private_mode_switch_is_local_no_model_and_sticks(monkeypatch):
    from kyraan.agents import orchestrator, session, agent_loop
    from kyraan.control_plane import kernel
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "memory")
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    seen = []
    async def fake_run(chat_id, raw_text, tier="frontier", read_only=False):
        seen.append((tier, False)); return "reply"
    monkeypatch.setattr(agent_loop, "run", fake_run)
    from kyraan.model_router import router
    def fake_call(**kw):
        seen.append((kw["tier"], True))
        class R: text = "private reply"
        return R()
    monkeypatch.setattr(router, "call", fake_call)
    monkeypatch.setattr(orchestrator, "log_chat", lambda *a, **k: None)
    session._history[57] = []
    secrets.set_private(57, False)
    out = asyncio.run(orchestrator.handle_message(57, "private mode on"))
    assert out.startswith("🔒 Private mode ON") and seen == []          # no model at all
    assert orchestrator.processing_marker(57).startswith("🔒")
    asyncio.run(orchestrator.handle_message(57, "how is my day looking"))
    assert seen[-1] == ("cheap", True)                                   # local, secret handling
    assert orchestrator.processing_marker(57).startswith("🔒")
    assert list(session._history[57])[-2:] == [("user", secrets.PLACEHOLDER), ("assistant", secrets.PLACEHOLDER)]
    out = asyncio.run(orchestrator.handle_message(57, "private mode off"))
    assert out.startswith("Private mode OFF")
    asyncio.run(orchestrator.handle_message(57, "how is my day looking"))
    assert seen[-1][0] == "frontier"
    assert orchestrator.processing_marker(57).startswith("☁️")
    session._history[57] = []


def test_private_turns_leave_no_text_in_traces(monkeypatch, tmp_path):
    from kyraan.control_plane import logging_setup as ls
    monkeypatch.setattr(ls, "TRACE_LOG", tmp_path / "t.jsonl")
    monkeypatch.setattr(ls, "EVENT_LOG", tmp_path / "e.jsonl")
    tok = ls.set_trace_redaction(secrets.PLACEHOLDER)
    try:
        ls.log_trace("model_io", prompt="i have a girlfriend", system="sys", latency_ms=12)
        ls.log_event("tool_call", args={"text": "i have a girlfriend"}, skill="x")
    finally:
        ls.reset_trace_redaction(tok)
    ls.log_trace("model_io", prompt="weather?", latency_ms=1)
    t = [json.loads(l) for l in (tmp_path / "t.jsonl").read_text().splitlines()]
    e = [json.loads(l) for l in (tmp_path / "e.jsonl").read_text().splitlines()]
    assert t[0]["prompt"] == secrets.PLACEHOLDER and t[0]["system"] == secrets.PLACEHOLDER and t[0]["latency_ms"] == 12
    assert e[0]["args"] == secrets.PLACEHOLDER and e[0]["skill"] == "x"
    assert t[1]["prompt"] == "weather?"                                  # normal turns untouched


def test_private_turn_without_a_local_answer_refuses_honestly(monkeypatch):
    from kyraan.agents import orchestrator, session, agent_loop
    from kyraan.control_plane import kernel
    from kyraan.model_router import router
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "memory")
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    called = []
    async def fake_run(*a, **k):
        called.append(1); return "loop"
    monkeypatch.setattr(agent_loop, "run", fake_run)
    class R: text = ""
    monkeypatch.setattr(router, "call", lambda **kw: R())
    monkeypatch.setattr(orchestrator, "log_chat", lambda *a, **k: None)
    session._history[58] = []
    secrets.set_private(58, True)
    out = asyncio.run(orchestrator.handle_message(58, "something private"))
    assert out.startswith("I couldn't get an answer from the local model") and called == []
    secrets.set_private(58, False)
    session._history[58] = []
