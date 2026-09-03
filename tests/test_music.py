"""Spotify (governance 2026-09-02): the media auto-exemption, device
resolution, volume caps, verified receipts, and honest unconfigured
states."""
import pytest

from kyraan.agents import loop_tools
from kyraan.control_plane import kernel
from kyraan.tools import spotify


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(spotify, "configured", lambda: True)


def test_media_exemption_is_exactly_eight_tools():
    from kyraan.tools import registry
    # music.skip joined 2026-09-03 ("next song" on the Echo)
    assert registry.MEDIA_AUTO_EXEMPT == {"music.play", "music.pause", "music.skip",
                                          "music.volume", "home.announce",
                                          "home.speaker_volume",
                                          "home.media", "home.tv_play"}
    # anything else notify+auto still refuses at load
    spec = registry.ToolSpec(
        name="x.blast", description="", server="spotify",
        permission="auto", side_effects="notify", params={}, returns="",
        retries=0, timeout_s=5, on_failure="silent")
    with pytest.raises(ValueError, match="forbidden"):
        registry._validate("x.blast", spec, {"x.blast"},
                           {"spotify": {"transport": "builtin",
                                        "module": "kyraan.tools.spotify"}})


async def test_play_resolves_device_and_verifies(monkeypatch):
    monkeypatch.setattr(spotify, "resolve_device",
                        lambda hint: {"id": "d1", "name": "Bedroom Echo"})
    monkeypatch.setattr(spotify, "search_uri",
                        lambda q, prefer="": {"uri": "spotify:track:t1",
                                              "kind": "track",
                                              "label": "Pal Pal — Kishore Kumar"})
    played = []
    monkeypatch.setattr(spotify, "play",
                        lambda uri, kind, dev: played.append((uri, dev)))
    monkeypatch.setattr(spotify, "player_state",
                        lambda: {"is_playing": True, "track": "Pal Pal"})
    out = await loop_tools._music_play(7, {"query": "kishore kumar",
                                           "device": "bedroom"}, "")
    assert played == [("spotify:track:t1", "d1")]
    assert out["verified"] is True and out["on"] == "Bedroom Echo"


async def test_play_with_no_device_names_the_online_ones(monkeypatch):
    monkeypatch.setattr(spotify, "resolve_device", lambda hint: None)
    monkeypatch.setattr(spotify, "devices",
                        lambda: [{"name": "Living Room Echo"}])
    with pytest.raises(kernel.ToolFailed, match="Living Room Echo"):
        await loop_tools._music_play(7, {"query": "x y"}, "")


async def test_volume_caps(monkeypatch):
    sets = []
    monkeypatch.setattr(spotify, "player_state", lambda: {"volume": 30})
    monkeypatch.setattr(spotify, "set_volume",
                        lambda p, d="": sets.append(p))
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: True)
    out = await loop_tools._music_volume(7, {"percent": 50}, "")
    assert sets == [50] and out["prior"] == 30
    with pytest.raises(kernel.ConfirmationRequired):     # >70 asks
        await loop_tools._music_volume(7, {"percent": 90}, "")
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: False)
    with pytest.raises(kernel.ConfirmationRequired):     # quiet hours: >40 asks
        await loop_tools._music_volume(7, {"percent": 50}, "")


async def test_unconfigured_is_an_honest_setup_pointer(monkeypatch):
    monkeypatch.setattr(spotify, "configured", lambda: False)
    with pytest.raises(kernel.ToolFailed, match="setup_spotify_oauth"):
        await loop_tools._music_play(7, {"query": "x y"}, "")


def test_volume_undo_restores_observed_prior():
    from kyraan.agents.loop_tools import UNDO_MAP
    assert UNDO_MAP["music.volume"]({}, {"volume": 90, "prior": 30}, None) == \
        ("music.volume", {"percent": 30})
    assert UNDO_MAP["music.volume"]({}, {"volume": 90, "prior": None}, None) is None
    assert UNDO_MAP["music.play"]({}, {}, None) == ("music.pause", {})


def test_music_tools_join_no_family_stage():
    assert not kernel.stage_allows("music.play", stage="full")
    assert not kernel.stage_allows("music.play", stage="read_mostly")


async def test_switch_already_in_state_never_asks(monkeypatch):
    calls = []

    async def fake_run(call, **kw):
        calls.append(call.tool_name)
        if call.tool_name == "home.get_state":
            return {"entity": call.args["entity"], "state": "on"}
        raise AssertionError("write should not run")
    monkeypatch.setattr(kernel, "run_tool", fake_run)
    out = await loop_tools.TOOLS["home.turn_on"]["run"](
        7, {"entity": "fan.air_purifier"}, "")
    assert out == {"changed": False, "state": "on",
                   "note": "already on — say so, don't ask to confirm a no-op"}
    assert calls == ["home.get_state"]


async def test_speaker_volume_understands_the_alexa_scale(monkeypatch):
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: True)
    ran = []

    async def fake_run(call, **kw):
        ran.append(dict(call.args))
        return {"volume": call.args["percent"], "on": "manab_s_echo_dot",
                "prior": 50}
    monkeypatch.setattr(kernel, "run_tool", fake_run)
    out = await loop_tools._speaker_volume(7, {"percent": 7}, "")
    assert ran[0]["percent"] == 70          # Alexa 7 -> 70%
    await loop_tools._speaker_volume(7, {"percent": 55}, "")
    assert ran[1]["percent"] == 55          # >10 is already percent
    with pytest.raises(kernel.ConfirmationRequired):
        await loop_tools._speaker_volume(7, {"percent": 9}, "")   # 90%: asks
    assert out["prior"] == 50


def test_speaker_volume_undo_restores_prior():
    from kyraan.agents.loop_tools import UNDO_MAP
    assert UNDO_MAP["home.speaker_volume"](
        {}, {"volume": 70, "prior": 50}, None) == \
        ("home.speaker_volume", {"percent": 50})


def test_playlist_preference_reorders_the_search(monkeypatch):
    pages = {"playlists": {"items": [{"uri": "spotify:playlist:p1",
                                      "name": "Kids Hindi Hits"}]},
             "tracks": {"items": [{"uri": "spotify:track:t1", "name": "One",
                                   "artists": [{"name": "A"}]}]}}
    monkeypatch.setattr(spotify, "_api", lambda path: pages)
    assert spotify.search_uri("kids hindi songs")["kind"] == "track"
    got = spotify.search_uri("kids hindi songs", prefer="playlist")
    assert got == {"uri": "spotify:playlist:p1", "kind": "playlist",
                   "label": "Kids Hindi Hits"}


async def test_play_kind_playlist_reaches_the_search(monkeypatch):
    seen = {}
    monkeypatch.setattr(spotify, "resolve_device",
                        lambda hint: {"id": "d1", "name": "Echo"})
    monkeypatch.setattr(spotify, "search_uri",
                        lambda q, prefer="": seen.update(prefer=prefer) or
                        {"uri": "u", "kind": "playlist", "label": "L"})
    monkeypatch.setattr(spotify, "play", lambda *a: None)
    monkeypatch.setattr(spotify, "player_state",
                        lambda: {"is_playing": True, "track": "x"})
    await loop_tools._music_play(7, {"query": "kids songs",
                                     "kind": "playlist"}, "")
    assert seen["prefer"] == "playlist"


async def test_same_volume_twice_in_a_turn_is_a_no_op(monkeypatch):
    """Live 2026-09-03 16:10: "echo volume 4" set 40% twice (target "" then
    "echo" — different args, so the repeat guard let it through)."""
    monkeypatch.setattr(kernel, "can_send_proactively", lambda **kw: True)
    ran = []

    async def fake_run(call, **kw):
        ran.append(dict(call.args))
        return {"volume": call.args["percent"], "on": "manab_s_echo_dot", "prior": 50}
    monkeypatch.setattr(kernel, "run_tool", fake_run)
    loop_tools._last_volume_set.pop(9, None)
    first = await loop_tools._speaker_volume(9, {"percent": 4}, "")
    second = await loop_tools._speaker_volume(9, {"percent": 4, "target": "echo"}, "")
    assert first["volume"] == 40 and second["changed"] is False and len(ran) == 1
    loop_tools._last_volume_set.pop(9, None)
