"""'Did you mean ...' for forgotten command phrases (owner 2026-09-03)."""
import asyncio

from kyraan.agents import commands


def test_near_misses_find_the_command():
    assert commands.suggest("index memory")[0][0] == "reindex vault"
    assert commands.suggest("index note")[0][0] == "reindex vault"
    assert commands.suggest("i forget that how to index the obsidian notes")[0][0] == "reindex vault"
    assert commands.suggest("check health")[0][0] == "health report"
    assert commands.suggest("merge duplicate memories")[0][0] == "consolidate memory"
    assert commands.suggest("how is the weather") == []
    assert commands.suggest("show me kiaan's photos") == []
    assert commands.suggest("note") == []                       # one loose word is not a command
    assert commands.suggest("undo")[0][0] == "undo"


def test_suggestion_then_yes_runs_the_phrase(monkeypatch):
    from kyraan.agents import orchestrator, session
    from kyraan.control_plane import kernel
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "memory")
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    monkeypatch.setattr(orchestrator, "log_chat", lambda *a, **k: None)
    ran = []
    from kyraan.store import notes
    monkeypatch.setattr(notes, "sync", lambda chat_id, root=None, force=False: ran.append(force) or {"indexed": 0, "unchanged": 0, "skipped": 0})
    session._history[61] = []
    out = asyncio.run(orchestrator.handle_message(61, "index memory"))
    assert out.startswith('Did you mean "reindex vault"')
    out = asyncio.run(orchestrator.handle_message(61, "yes"))
    assert ran == [True] and "Did you mean" not in out      # the phrase ran
    out = asyncio.run(orchestrator.handle_message(61, "help"))
    assert out.startswith("Exact commands I always understand") and '"reindex vault"' in out
    session._history[61] = []


def test_brief_names_the_exact_commands():
    from kyraan.agents.capabilities import capability_brief
    b = capability_brief()
    assert "EXACT COMMANDS" in b and '"reindex vault"' in b and '"help"' in b
