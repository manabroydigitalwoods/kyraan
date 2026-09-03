"""'next song' skips the Spotify track (live 2026-09-03 15:58)."""
import asyncio


def test_skip_executor_verifies_the_track_changed(monkeypatch):
    from kyraan.agents import loop_tools
    from kyraan.tools import spotify
    calls = []
    tracks = iter(["Nani Teri Morni", "Lakdi Ki Kathi"])
    monkeypatch.setattr(spotify, "configured", lambda: True)
    monkeypatch.setattr(spotify, "skip", lambda d: calls.append(d))
    monkeypatch.setattr(spotify, "player_state", lambda: {"track": next(tracks), "device": "Manab's Echo Dot", "is_playing": True})
    out = asyncio.run(loop_tools._music_skip(1, {"direction": "next"}, ""))
    assert calls == ["next"] and out["verified"] is True and out["now"] == "Lakdi Ki Kathi"


def test_next_song_is_deterministic(monkeypatch):
    from kyraan.agents import orchestrator, loop_tools
    from kyraan.control_plane import kernel
    from kyraan.tools import spotify
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    monkeypatch.setattr(spotify, "configured", lambda: True)
    seen = []

    async def fake_skip(chat_id, args, raw_text):
        seen.append(args["direction"])
        return {"skipped": args["direction"], "now": "X", "on": "Echo", "verified": True}
    monkeypatch.setattr(loop_tools, "_music_skip", fake_skip)
    for q, want in (("next song", "next"), ("skip", "next"), ("previous track", "previous"),
                    ("go back one song", "previous"), ("Next", "next")):
        out = asyncio.run(orchestrator.handle_message(1, q))
        assert out.startswith("Skipped to the"), q
    assert seen == ["next", "next", "previous", "previous", "next"]


def test_claude_is_found_on_the_launchd_path(monkeypatch):
    from kyraan.tools import code_agent as ca
    monkeypatch.setenv("PATH", "/usr/bin:/bin")          # what launchd gives the service
    assert ca.claude_binary() is None or ca.claude_binary().startswith(("/opt/homebrew", "/usr/local"))
    assert "/opt/homebrew/bin" in ca._clean_env()["PATH"]
