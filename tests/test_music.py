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


def test_media_exemption_is_exactly_four_tools():
    from kyraan.tools import registry
    assert registry.MEDIA_AUTO_EXEMPT == {"music.play", "music.pause",
                                          "music.volume", "home.announce"}
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
                        lambda q: {"uri": "spotify:track:t1", "kind": "track",
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
