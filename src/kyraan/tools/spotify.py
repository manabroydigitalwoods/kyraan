"""Spotify adapter (governance round 2026-09-02): official Web API,
playback on the owner's Connect devices — Echos included, which is how
"play Kishore Kumar in the bedroom" reaches the speaker without any
Alexa API. Owner's decisions: play/pause/volume are auto (the named
MEDIA_AUTO_EXEMPT in the registry — audible actions verify
themselves); volume confirms above 70% (40% in quiet hours, enforced
in the executor); any device on the owner's account is a target.

Setup: SPOTIFY_CLIENT_ID/SECRET in .env, then
scripts/setup_spotify_oauth.py once (Premium required for Connect
control). Refresh token lives in data/spotify_token.json (0600).
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kyraan.tools.registry import ToolError, TransientToolError

TOKEN_PATH = Path(__file__).resolve().parents[3] / "data" / "spotify_token.json"
_API = "https://api.spotify.com/v1"
_TOKEN_URL = "https://accounts.spotify.com/api/token"

_lock = threading.Lock()
_cached: tuple | None = None  # (access_token, expires_at)


def configured() -> bool:
    return TOKEN_PATH.exists() and bool(os.environ.get("SPOTIFY_CLIENT_ID"))


def _refresh_token() -> str:
    try:
        return json.loads(TOKEN_PATH.read_text())["refresh_token"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise ToolError("Spotify isn't connected — run "
                        "scripts/setup_spotify_oauth.py once") from exc


def access_token() -> str:
    global _cached
    with _lock:
        if _cached and _cached[1] > time.time() + 60:
            return _cached[0]
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": _refresh_token(),
            "client_id": os.environ.get("SPOTIFY_CLIENT_ID", ""),
            "client_secret": os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
        }).encode()
        request = urllib.request.Request(_TOKEN_URL, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise ToolError(f"Spotify token refresh failed ({exc.code}) — "
                            "re-run scripts/setup_spotify_oauth.py") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TransientToolError(f"could not reach Spotify auth: {exc}") from exc
        _cached = (data["access_token"], time.time() + data.get("expires_in", 3600))
        return _cached[0]


def _api(path: str, method: str = "GET", payload: dict | None = None):
    request = urllib.request.Request(
        f"{_API}{path}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {access_token()}",
                 **({"Content-Type": "application/json"}
                    if payload is not None else {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise ToolError("Spotify refused (403) — Connect control needs "
                            "Premium on this account") from exc
        if exc.code == 404:
            raise ToolError("no active Spotify session — for the ECHO's "
                            "own volume use home.speaker_volume; to play "
                            "first, open Spotify or say what to play") from exc
        if exc.code == 429:
            raise TransientToolError("Spotify rate limit") from exc
        if exc.code >= 500:
            raise TransientToolError(f"Spotify returned {exc.code}") from exc
        raise ToolError(f"Spotify returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Spotify: {exc}") from exc


def devices() -> list:
    return [{"id": d["id"], "name": d["name"], "type": d["type"],
             "active": d.get("is_active", False),
             "volume": d.get("volume_percent")}
            for d in _api("/me/player/devices").get("devices", [])]


def resolve_device(name_hint: str) -> dict | None:
    """Named device by substring; no hint = the active one, else the
    first available. None when nothing is online."""
    online = devices()
    if not online:
        return None
    if name_hint:
        hint = name_hint.lower()
        for d in online:
            if hint in d["name"].lower():
                return d
        return None
    return next((d for d in online if d["active"]), online[0])


def search_uri(query: str, prefer: str = "") -> dict | None:
    """Best playable match: track first, then playlist, then artist —
    unless the caller prefers playlists ("play all the kids songs" is
    a playlist ask, not a which-track interrogation)."""
    q = urllib.parse.quote(query)
    found = _api(f"/search?q={q}&type=track,playlist,artist&limit=3")
    order = (("playlists", "tracks", "artists") if prefer == "playlist"
             else ("tracks", "playlists", "artists"))
    for kind in order:
        for item in (found.get(kind) or {}).get("items") or []:
            if not item:
                continue
            label = item.get("name", "")
            if kind == "tracks":
                artists = ", ".join(a["name"] for a in item.get("artists", []))
                return {"uri": item["uri"], "kind": "track",
                        "label": f"{label} — {artists}"}
            return {"uri": item["uri"], "kind": kind[:-1], "label": label}
    return None


def play(uri: str, kind: str, device_id: str) -> None:
    payload = {"uris": [uri]} if kind == "track" else {"context_uri": uri}
    _api(f"/me/player/play?device_id={device_id}", "PUT", payload)


def pause() -> None:
    _api("/me/player/pause", "PUT", {})


def set_volume(percent: int, device_id: str = "") -> None:
    extra = f"&device_id={device_id}" if device_id else ""
    _api(f"/me/player/volume?volume_percent={int(percent)}{extra}", "PUT", {})


def player_state() -> dict:
    """The verification read: what is ACTUALLY playing right now."""
    state = _api("/me/player") or {}
    item = state.get("item") or {}
    return {"is_playing": bool(state.get("is_playing")),
            "track": item.get("name", ""),
            "device": (state.get("device") or {}).get("name", ""),
            "volume": (state.get("device") or {}).get("volume_percent")}


async def call(tool_name: str, args: dict) -> object:
    import asyncio
    if tool_name == "music.devices":
        return await asyncio.to_thread(devices)
    raise ToolError(f"spotify adapter does not provide {tool_name!r}")
