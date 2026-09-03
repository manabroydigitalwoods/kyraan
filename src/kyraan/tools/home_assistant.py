"""Home Assistant adapter — tool #2. Talks to the local HA instance's REST
API (long-lived token in .env). Every entity Kyraan may touch is named in
an explicit allowlist in permissions.yaml's tool_servers section: reads
against read_entities, writes against write_entities — an entity not
listed does not exist as far as Kyraan is concerned, and adding one is a
deliberate config edit, never discovery. v1 scope: the bedroom AC smart
plug (owner's call — heater/geyser/vacuum join later, one line each).

Adapter contract (docs/design/tool_registry.md): `async def call(tool_name, args)`.
"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.request

from kyraan.control_plane import config
from kyraan.tools.registry import ToolError, TransientToolError


def _base() -> tuple[str, str]:
    url = os.environ.get("HASS_URL", "").strip().rstrip("/")
    token = os.environ.get("HASS_TOKEN", "").strip()
    if not (url and token):
        raise ToolError(
            "Home Assistant isn't configured — set HASS_URL and HASS_TOKEN in .env "
            "(HA profile → Security → Long-lived access tokens)"
        )
    return url, token


def _allowlists() -> tuple[list, list]:
    server = (config.load().get("tool_servers") or {}).get("home_assistant") or {}
    return server.get("read_entities") or [], server.get("write_entities") or []


def _api(path: str, payload: dict | None = None) -> object:
    url, token = _base()
    request = urllib.request.Request(
        f"{url}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ToolError("Home Assistant rejected the token — regenerate it in HA and update .env") from exc
        if exc.code == 404:
            raise ToolError(f"Home Assistant doesn't know this entity ({path})") from exc
        if exc.code >= 500:
            raise TransientToolError(f"Home Assistant returned {exc.code}") from exc
        raise ToolError(f"Home Assistant error {exc.code} on {path}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Home Assistant: {exc}") from exc


def _get_state(entity: str) -> dict:
    reads, writes = _allowlists()
    if entity not in reads and entity not in writes:
        raise ToolError(f"entity {entity!r} is not in Kyraan's allowlist (tool_servers.home_assistant)")
    data = _api(f"/api/states/{entity}")
    return {
        "entity": entity,
        "state": data["state"],
        "unit": data.get("attributes", {}).get("unit_of_measurement"),
        "name": data.get("attributes", {}).get("friendly_name", entity),
        # ISO timestamp of the last state flip — lets callers answer
        # "how long has it been on?" honestly from HA's own records.
        "last_changed": data.get("last_changed"),
    }


def _switch(entity: str, turn_on: bool) -> dict:
    _, writes = _allowlists()
    if entity not in writes:
        raise ToolError(
            f"entity {entity!r} is not write-allowlisted — switchable "
            "entities are EXACTLY: " + (", ".join(writes) or "(none)"))
    domain = entity.split(".")[0]
    if domain not in ("switch", "fan", "media_player", "light"):
        # fan joined 2026-09-02 (the Philips purifier); media_player the
        # same day (the Fire TV); light 2026-09-03 (the purifier's display
        # backlight) — identical turn_on/turn_off services.
        raise ToolError(f"only switch/fan/media_player/light entities are "
                        f"switchable; {entity!r} is a {domain}")
    _api(f"/api/services/{domain}/turn_{'on' if turn_on else 'off'}", {"entity_id": entity})
    # Read back — report what the device actually did, never assume. HA
    # applies service calls asynchronously, so the immediate read returns
    # the PRE-switch state (seen live: confirmed ON, reply said OFF). Poll
    # briefly until the state converges; an unconverged result is returned
    # as-is with converged=False so the caller can be honest about it.
    # media_player states aren't binary: a woken Fire TV reports
    # idle/playing, a sleeping one standby — judge by state FAMILY.
    on_family = {"on", "idle", "playing", "paused", "buffering"}
    def _matches(value: str) -> bool:
        if domain == "media_player":
            return (value in on_family) == turn_on
        return value == ("on" if turn_on else "off")
    state = {}
    for _ in range(10):
        state = _get_state(entity)
        if _matches(state["state"]):
            state["converged"] = True
            return state
        time.sleep(0.6)
    state["converged"] = False
    return state


def _announce_targets() -> list:
    server = (config.load().get("tool_servers") or {}).get("home_assistant") or {}
    return server.get("announce_targets") or []


def _announce(message: str, target: str = "") -> dict:
    """Speak through an Echo via Alexa Media Player's notify service
    (governance 2026-09-02: auto, DND-gated in the executor). Targets
    are ALLOWLISTED like entities — an un-listed speaker doesn't exist."""
    targets = _announce_targets()
    if not targets:
        raise ToolError("no announce_targets configured — add Echo names "
                        "under tool_servers.home_assistant in permissions.yaml")
    chosen = targets[0]
    if target:
        hint = target.strip().lower().replace(" ", "_")
        chosen = next((t for t in targets if hint in t.lower()), None)
        if chosen is None:
            raise ToolError(f"unknown speaker {target!r} — configured: "
                            + ", ".join(targets))
    _api(f"/api/services/notify/alexa_media_{chosen}",
         {"message": message, "data": {"type": "announce"}})
    return {"announced": True, "on": chosen, "message": message}


def _speaker_volume(percent: int, target: str = "") -> dict:
    """Echo DEVICE volume via media_player.volume_set — what
    announcements and Alexa-played audio use (distinct from Spotify's
    playback volume). Targets ride the same announce allowlist."""
    targets = _announce_targets() + [
        e.split(".", 1)[1] for e in _media_players()]
    if not targets:
        raise ToolError("no announce_targets configured")
    chosen = targets[0]
    if target:
        hint = target.strip().lower().replace(" ", "_")
        chosen = next((t for t in targets if hint in t.lower()), None)
        if chosen is None:
            raise ToolError(f"unknown speaker {target!r} — configured: "
                            + ", ".join(targets))
    entity = f"media_player.{chosen}"
    prior = None
    try:
        raw = _api(f"/api/states/{entity}")
        prior = raw.get("attributes", {}).get("volume_level")
        prior = round(prior * 100) if prior is not None else None
    except Exception:
        pass
    _api("/api/services/media_player/volume_set",
         {"entity_id": entity, "volume_level": round(percent / 100, 2)})
    return {"volume": percent, "on": chosen, "prior": prior}


_TRANSPORT = {"play": "media_play", "pause": "media_pause",
              "stop": "media_stop", "next": "media_next_track",
              "previous": "media_previous_track"}


def _media_players() -> list:
    _, writes = _allowlists()
    return [e for e in writes if e.startswith("media_player.")]


def _media_transport(action: str, target: str = "") -> dict:
    """play/pause/next/previous/stop on an allowlisted media player —
    the Fire TV's native remote, no Alexa voice involved."""
    service = _TRANSPORT.get(action)
    if service is None:
        raise ToolError(f"action must be one of {sorted(_TRANSPORT)}")
    players = _media_players()
    if not players:
        raise ToolError("no media players are write-allowlisted")
    entity = players[0]
    if target:
        hint = target.strip().lower().replace(" ", "_")
        entity = next((e for e in players if hint in e), None)
        if entity is None:
            raise ToolError(f"unknown player {target!r} — allowlisted: "
                            + ", ".join(players))
    _api(f"/api/services/media_player/{service}", {"entity_id": entity})
    return {"action": action, "on": entity}


_VOICE_APPS = ("netflix", "prime video", "youtube")
_VOICE_TITLE = None  # compiled lazily


def _alexa_play_title(title: str, app: str) -> dict:
    """The ENVELOPED voice bridge (owner 'go', 2026-09-02): the ONLY
    thing this can utter is 'play <title> on <app> on fire tv' — title
    pattern-checked, app from a fixed tuple. Nothing else can transit;
    the general Alexa surface stays closed."""
    import re
    global _VOICE_TITLE
    if _VOICE_TITLE is None:
        _VOICE_TITLE = re.compile(r"^[\w \'&:,.!-]{2,60}$")
    app = app.strip().lower()
    if app not in _VOICE_APPS:
        raise ToolError(f"app must be one of {_VOICE_APPS}")
    title = " ".join(str(title or "").split())
    if not _VOICE_TITLE.match(title) or re.search(
            r"\b(?:and|then|also|order|buy|call|send|alexa)\b",
            title, re.IGNORECASE):
        raise ToolError("that title doesn't fit the play envelope — plain "
                        "title words only")
    targets = _announce_targets()
    if not targets:
        raise ToolError("no announce_targets configured")
    phrase = f"play {title} on {app} on fire tv"
    _api("/api/services/media_player/play_media",
         {"entity_id": f"media_player.{targets[0]}",
          "media_content_type": "custom", "media_content_id": phrase})
    return {"asked_alexa": phrase, "note": "Alexa resolves the title — "
            "relay that it was requested, not confirmed playing"}


PURIFIER_FAN = "fan.air_purifier"
PURIFIER_INDEX = "select.air_purifier_preferred_index"
PURIFIER_TIMER = "select.air_purifier_timer"


def _raw(entity: str) -> dict:
    """An allowlisted entity's FULL HA record — _get_state trims the
    attributes, and the purifier's modes/options live there."""
    reads, writes = _allowlists()
    if entity not in reads and entity not in writes:
        raise ToolError(f"entity {entity!r} is not in Kyraan's allowlist (tool_servers.home_assistant)")
    return _api(f"/api/states/{entity}")


def purifier_state() -> dict:
    """Mode, timer and index as the device reports them (plus what it
    offers) — the read the write is checked against."""
    fan = _raw(PURIFIER_FAN)
    attrs = fan.get("attributes") or {}
    out = {"entity": PURIFIER_FAN, "power": fan.get("state"),
           "mode": attrs.get("preset_mode"), "modes": list(attrs.get("preset_modes") or [])}
    for key, entity in (("timer", PURIFIER_TIMER), ("index", PURIFIER_INDEX)):
        try:
            st = _raw(entity)
            out[key] = st.get("state")
            out[f"{key}_options"] = list((st.get("attributes") or {}).get("options") or [])
        except Exception:
            out[key], out[f"{key}_options"] = None, []
    return out


def _purifier(mode: str = "", timer: str = "", index: str = "") -> dict:
    """Set any subset of mode / timer / index on the Philips purifier
    (owner 2026-09-03). Values are validated against what the device
    OFFERS right now (HA's preset_modes / select options), not a list
    we hard-code; the result is read back until it converges."""
    _, writes = _allowlists()
    if PURIFIER_FAN not in writes:
        raise ToolError("the air purifier is not write-allowlisted")
    current = purifier_state()
    wanted = {}
    if mode:
        m = mode.strip().lower()
        if m not in current["modes"]:
            raise ToolError(f"mode must be one of {', '.join(current['modes'])} — not {mode!r}")
        wanted["mode"] = m
    if timer:
        t = timer.strip().lower().replace(" ", "")
        options = {o.lower(): o for o in current.get("timer_options") or []}
        if t in ("0", "none", "cancel"):
            t = "off"
        if t not in options:
            raise ToolError(f"timer must be one of {', '.join(options.values())} — not {timer!r}")
        wanted["timer"] = options[t]
    if index:
        i = index.strip().lower().replace(" ", "_").replace("pm2.5", "pm25")
        options = {o.lower(): o for o in current.get("index_options") or []}
        if i not in options:
            raise ToolError(f"index must be one of {', '.join(options.values())} — not {index!r}")
        wanted["index"] = options[i]
    if not wanted:
        raise ToolError("say what to set: mode (auto/turbo/medium/sleep), timer (1h..12h/off) or index")
    if "mode" in wanted and wanted["mode"] != current["mode"]:
        if current["power"] != "on":
            _api("/api/services/fan/turn_on", {"entity_id": PURIFIER_FAN})
        _api("/api/services/fan/set_preset_mode",
             {"entity_id": PURIFIER_FAN, "preset_mode": wanted["mode"]})
    if "timer" in wanted and wanted["timer"] != current["timer"]:
        _api("/api/services/select/select_option",
             {"entity_id": PURIFIER_TIMER, "option": wanted["timer"]})
    if "index" in wanted and wanted["index"] != current["index"]:
        _api("/api/services/select/select_option",
             {"entity_id": PURIFIER_INDEX, "option": wanted["index"]})
    state = current
    for _ in range(10):
        state = purifier_state()
        if all(state.get(k) == v for k, v in wanted.items()):
            return {**{k: state.get(k) for k in ("entity", "power", "mode", "timer", "index")},
                    "requested": wanted, "converged": True}
        time.sleep(0.6)
    return {**{k: state.get(k) for k in ("entity", "power", "mode", "timer", "index")},
            "requested": wanted, "converged": False}


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "home.media":
        return await asyncio.to_thread(_media_transport, str(args["action"]),
                                       str(args.get("target", "") or ""))
    if tool_name == "home.tv_play":
        return await asyncio.to_thread(_alexa_play_title,
                                       str(args.get("title", "")),
                                       str(args.get("app", "")))
    if tool_name == "home.speaker_volume":
        return await asyncio.to_thread(_speaker_volume, int(args["percent"]),
                                       str(args.get("target", "") or ""))
    if tool_name == "home.announce":
        return await asyncio.to_thread(_announce, args["message"],
                                       str(args.get("target", "") or ""))
    if tool_name == "home.get_state":
        return await asyncio.to_thread(_get_state, args["entity"])
    if tool_name == "home.purifier":
        return await asyncio.to_thread(_purifier, str(args.get("mode", "") or ""),
                                       str(args.get("timer", "") or ""),
                                       str(args.get("index", "") or ""))
    if tool_name == "home.turn_on":
        return await asyncio.to_thread(_switch, args["entity"], True)
    if tool_name == "home.turn_off":
        return await asyncio.to_thread(_switch, args["entity"], False)
    raise ToolError(f"home_assistant adapter does not provide {tool_name!r}")
