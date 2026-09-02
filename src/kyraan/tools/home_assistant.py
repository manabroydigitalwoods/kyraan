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
        raise ToolError(f"entity {entity!r} is not write-allowlisted — Kyraan may not switch it")
    domain = entity.split(".")[0]
    if domain != "switch":
        raise ToolError(f"only switch entities are switchable; {entity!r} is a {domain}")
    _api(f"/api/services/switch/turn_{'on' if turn_on else 'off'}", {"entity_id": entity})
    # Read back — report what the device actually did, never assume. HA
    # applies service calls asynchronously, so the immediate read returns
    # the PRE-switch state (seen live: confirmed ON, reply said OFF). Poll
    # briefly until the state converges; an unconverged result is returned
    # as-is with converged=False so the caller can be honest about it.
    expected = "on" if turn_on else "off"
    state = {}
    for _ in range(10):
        state = _get_state(entity)
        if state["state"] == expected:
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


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "home.announce":
        return await asyncio.to_thread(_announce, args["message"],
                                       str(args.get("target", "") or ""))
    if tool_name == "home.get_state":
        return await asyncio.to_thread(_get_state, args["entity"])
    if tool_name == "home.turn_on":
        return await asyncio.to_thread(_switch, args["entity"], True)
    if tool_name == "home.turn_off":
        return await asyncio.to_thread(_switch, args["entity"], False)
    raise ToolError(f"home_assistant adapter does not provide {tool_name!r}")
