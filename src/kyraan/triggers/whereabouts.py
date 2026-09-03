"""Whereabouts — location awareness (owner 2026-09-04, "go with 1 then 3").

Jarvis knows where Tony is. Telegram gives Kyraan a pin when the owner
shares one, and — with "share live location" — a stream of edits for up
to eight hours. Until now the initial pin became a message and the
edits were dropped. This module keeps the owner's last fix and
notices transitions:

  * HOMEWARD: farther than 3 km from home and now inside it, closing
    in — once per trip: "about N minutes from home — AC on?" (the
    question is the confirm gate for switch.ac; "yes" turns it on).
  * A KNOWN PLACE: the owner names places ("remember this place as the
    clinic", from the last fix); arriving at one says what Kyraan holds
    about it — documents and notes that mention the name — once per
    visit.
  * "where am I" answers from the last fix.

Home is Home Assistant's zone.home. Coordinates stay in one local
state file; nothing here reaches a cloud prompt — the transitions are
deterministic and the messages are composed here.
"""
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from kyraan.control_plane import kernel
from kyraan.control_plane.filelock import atomic_write_text
from kyraan.control_plane.logging_setup import log_event

STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "duties" / "whereabouts.json"
HOMEWARD_KM = 3.0          # the "nearly home" ring
HOME_RADIUS_KM = 0.15
PLACE_RADIUS_KM = 0.25
TRIP_RESET_KM = 5.0        # this far out = a new trip, re-arm the homeward nudge
FIX_STALE_S = 3 * 3600


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"last": None, "places": {}, "armed": {"homeward": True}, "visits": {}}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False))


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_home_cache: dict = {}


def home() -> tuple | None:
    """(lat, lon) of Home Assistant's zone.home, cached an hour."""
    if _home_cache and time.time() - _home_cache.get("at", 0) < 3600:
        return _home_cache.get("pos")
    pos = None
    try:
        import os
        import urllib.request
        url = os.environ.get("HASS_URL", "").rstrip("/")
        tok = os.environ.get("HASS_TOKEN", "")
        if url and tok:
            req = urllib.request.Request(f"{url}/api/states/zone.home",
                                         headers={"Authorization": f"Bearer {tok}"})
            data = json.load(urllib.request.urlopen(req, timeout=6))
            a = data.get("attributes") or {}
            pos = (float(a["latitude"]), float(a["longitude"]))
    except Exception as exc:
        log_event("whereabouts_home_unavailable", error=str(exc)[:80])
    _home_cache.update(at=time.time(), pos=pos)
    return pos


# ---------------------------------------------------------------- fixes --

def observe(lat: float, lon: float, when: float | None = None) -> list:
    """Record a fix; return the proactive lines it earns (usually none).
    Pure apart from the state file and the home lookup."""
    when = when or time.time()
    state = _load()
    last = state.get("last")
    state["last"] = {"lat": lat, "lon": lon, "at": when}
    lines = []
    hm = home()
    armed = state.setdefault("armed", {"homeward": True})
    if hm:
        d = km(lat, lon, *hm)
        prev_d = km(last["lat"], last["lon"], *hm) if last else None
        if d > TRIP_RESET_KM:
            armed["homeward"] = True
        elif (armed.get("homeward", True) and d <= HOMEWARD_KM and d > HOME_RADIUS_KM
              and prev_d is not None and prev_d > d + 0.2):
            minutes = max(2, int(d / 25 * 60))    # town driving pace
            lines.append(("homeward", f"You're about {minutes} minutes from home ({d:.1f} km)."))
            armed["homeward"] = False
    visits = state.setdefault("visits", {})
    for name, place in (state.get("places") or {}).items():
        d = km(lat, lon, place["lat"], place["lon"])
        inside = d <= place.get("radius_km", PLACE_RADIUS_KM)
        was = visits.get(name, {}).get("inside", False)
        if inside and not was:
            lines.append(("arrived", name))
        visits[name] = {"inside": inside, "at": when}
    _save(state)
    return lines


def last_fix() -> dict | None:
    st = _load().get("last")
    if not st or time.time() - st.get("at", 0) > FIX_STALE_S:
        return None
    return st


def remember_place(name: str, radius_km: float = PLACE_RADIUS_KM) -> dict | None:
    fix = last_fix()
    if not fix:
        return None
    state = _load()
    state.setdefault("places", {})[name.strip().lower()] = {
        "lat": fix["lat"], "lon": fix["lon"], "radius_km": radius_km,
        "since": datetime.now(timezone.utc).isoformat()}
    _save(state)
    log_event("whereabouts_place_remembered", place=name.strip().lower())
    return state["places"][name.strip().lower()]


def forget_place(name: str) -> bool:
    state = _load()
    gone = state.get("places", {}).pop(name.strip().lower(), None) is not None
    _save(state)
    return gone


def places() -> dict:
    return _load().get("places", {})


def linked_to(place: str, chat_id: int, limit: int = 3) -> list:
    """Documents and notes that mention the place by name."""
    try:
        from kyraan.store import pg
        with pg.connection() as conn:
            rows = conn.execute(
                """SELECT kind, caption FROM document WHERE chat_id = %s AND suppressed_by = '{}'
                   AND exposure = 'cloud_ok'
                   AND (caption ILIKE %s OR text ILIKE %s OR %s = ANY(entities))
                   ORDER BY created_at DESC LIMIT %s""",
                (chat_id, f"%{place}%", f"%{place}%", place, limit)).fetchall()
        return [f"{k}: {c}" for k, c in rows]
    except Exception:
        return []


def where_text() -> str:
    fix = last_fix()
    if not fix:
        return ("I don't have a recent location from you — share a pin (or live location) "
                "in Telegram and I'll keep track.")
    try:
        from kyraan.channels import location as geo
        described = geo.describe(fix["lat"], fix["lon"])
    except Exception:
        described = f"{fix['lat']:.5f}, {fix['lon']:.5f}"
    age = int((time.time() - fix["at"]) / 60)
    hm = home()
    dist = f", {km(fix['lat'], fix['lon'], *hm):.1f} km from home" if hm else ""
    near = [n for n, p in places().items()
            if km(fix["lat"], fix["lon"], p["lat"], p["lon"]) <= p.get("radius_km", PLACE_RADIUS_KM)]
    return (f"Last I knew ({age} min ago): {described}{dist}."
            + (f" That's your saved place: {', '.join(near)}." if near else ""))


# ------------------------------------------------------------- proactive --

async def announce(chat_id: int, lines: list, send_fn) -> int:
    """Turn observe()'s transitions into messages — the homeward one is
    a confirm ask for the AC when the AC is off; a known place lists
    what Kyraan holds about it."""
    if not lines or not kernel.can_send_proactively(chat_id=chat_id):
        return 0
    sent = 0
    for kind, payload in lines:
        if kind == "homeward":
            text = payload
            try:
                ac = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": "switch.ac"}), meta=True)
                if isinstance(ac, dict) and ac.get("state") == "off":
                    from kyraan.agents import orchestrator
                    from kyraan.control_plane.kernel import SkillCall

                    async def _ac_on(_a: dict) -> str:
                        r = await kernel.run_tool(kernel.ToolCall("home.turn_on", {"entity": "switch.ac"}))
                        return "AC on — the room will be cool by the time you're in." if (
                            isinstance(r, dict) and r.get("state") == "on") else "I sent the AC on — check it in a moment."
                    text = await orchestrator._gated(
                        chat_id, SkillCall("home.turn_on", {"entity": "switch.ac"}), _ac_on,
                        describe=f"{payload} Turn the AC on so the room is cool when you get in")
            except Exception as exc:
                log_event("whereabouts_ac_check_failed", error=str(exc)[:80])
        else:
            held = linked_to(payload, chat_id)
            text = f"📍 You're at {payload}." + (
                "\nI have: " + "; ".join(held) + ' — say "show <name>" for any of them.' if held else "")
        if await send_fn(chat_id, text) is not False:
            sent += 1
            log_event("whereabouts_sent", chat_id=chat_id, what=kind)
    return sent


PERSON_ENTITY = "person.manab_roy"
MAX_ACCURACY_M = 250
_person_seen: dict = {}


async def poll_person(chat_id: int, send_fn) -> int:
    """Home Assistant's person entity — the companion app's tracker once
    the app is installed (2026-09-04: the entity exists, no tracker yet,
    so this stays silent). A new coordinate is a fix like a Telegram pin."""
    try:
        import asyncio
        from kyraan.tools import home_assistant as ha
        raw = await asyncio.to_thread(ha._raw, PERSON_ENTITY)
    except Exception:
        return 0
    attrs = raw.get("attributes") or {}
    lat, lon = attrs.get("latitude"), attrs.get("longitude")
    if lat is None or lon is None:
        return 0
    try:
        acc = float(attrs.get("gps_accuracy") or 0)
    except (TypeError, ValueError):
        acc = 0
    if acc > MAX_ACCURACY_M:
        # the app's first report was a coarse one (live 2026-09-04 02:48:
        # "8.2 km from home" while the owner sat at home); a rough fix is
        # not a whereabouts
        log_event("whereabouts_fix_rejected", accuracy_m=int(acc))
        return 0
    stamp = str(raw.get("last_updated") or "")
    if _person_seen.get("stamp") == stamp:
        return 0
    _person_seen["stamp"] = stamp
    lines = observe(float(lat), float(lon))
    log_event("whereabouts_person_fix", source="home_assistant")
    return await announce(chat_id, lines, send_fn)
