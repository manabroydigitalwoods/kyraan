"""Travel-time adapter — tool #7 (2026-08-26). Google Routes API primary,
TomTom Routing fallback: distance + duration with LIVE traffic vs
free-flow, so "how's traffic to Jalpaiguri?" answers as a human wants it
— "42 min right now, ~12 more than usual". GOOGLE_MAPS_API_KEY is the
same key places uses (restrict it to Places API (New) + Routes API);
TOMTOM_API_KEY (free tier, no card) serves when Google fails or when
only it is set.

Both backends carry real live traffic — that's the whole fallback rule:
a silently traffic-blind ETA ("35 minutes" from static data) would be
worse than an honest "can't reach traffic data", so nothing traffic-blind
ever answers here.

Adapter contract (docs/design/tool_registry.md): `async def call(tool_name, args)`.
"""
import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from kyraan.tools.registry import ToolError, TransientToolError

_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"
_MODES = {"drive": "DRIVE", "two_wheeler": "TWO_WHEELER", "walk": "WALK"}
_TOMTOM_MODES = {"drive": "car", "two_wheeler": "motorcycle", "walk": "pedestrian"}
_TOMTOM_ROUTE = "https://api.tomtom.com/routing/1/calculateRoute"
# /search (fuzzy, includes POIs), NOT /geocode (addresses only): a mall
# or landmark name doesn't exist for the geocode endpoint, which then
# matched a same-named address 1671 km away (live 2026-08-26).
_TOMTOM_GEOCODE = "https://api.tomtom.com/search/2/search"


def _google_key() -> str:
    return os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()


def _tomtom_key() -> str:
    return os.environ.get("TOMTOM_API_KEY", "").strip()


def configured() -> bool:
    return bool(_google_key() or _tomtom_key())


def _tomtom_point(args: dict, side: str, key: str) -> str:
    """TomTom routes between coordinates only — geocode names via its
    Search API (same key, same free tier). Unbiased, its global geocoder
    happily picks a same-named place on another continent ("City Center
    Mall, Siliguri" resolved to Ohio, USA — live 2026-08-26), so bias by
    the home country (KYRAAN_HOME_COUNTRY) and, when the other endpoint
    has pin coordinates, by those too."""
    lat, lon = args.get(f"{side}_latitude"), args.get(f"{side}_longitude")
    if lat is not None and lon is not None:
        return f"{float(lat):.5f},{float(lon):.5f}"
    place = str(args.get(side, "") or "").strip()
    if not place:
        raise ToolError(f"routes.eta needs an {side} — a place name, or lat/lon "
                        "from the user's shared pin")
    params = {"key": key, "limit": 1}
    country = os.environ.get("KYRAAN_HOME_COUNTRY", "").strip()
    if country:
        params["countrySet"] = country
    other = "destination" if side == "origin" else "origin"
    olat, olon = args.get(f"{other}_latitude"), args.get(f"{other}_longitude")
    if olat is not None and olon is not None:
        params["lat"], params["lon"] = f"{float(olat):.5f}", f"{float(olon):.5f}"
    url = (f"{_TOMTOM_GEOCODE}/{urllib.parse.quote(place)}.json?"
           + urllib.parse.urlencode(params))
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ToolError("TomTom rejected the API key — check TOMTOM_API_KEY") from exc
        if exc.code == 429 or exc.code >= 500:
            raise TransientToolError(f"TomTom geocoder returned {exc.code}") from exc
        raise ToolError(f"TomTom geocoder error {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach TomTom: {exc}") from exc
    results = data.get("results") or []
    if not results:
        raise ToolError(f"no place named {place!r} found — try a nearby town or landmark")
    pos = results[0].get("position") or {}
    return f"{pos.get('lat')},{pos.get('lon')}"


def _tomtom_eta(args: dict, mode_key: str, key: str) -> dict:
    origin = _tomtom_point(args, "origin", key)
    destination = _tomtom_point(args, "destination", key)
    url = (f"{_TOMTOM_ROUTE}/{origin}:{destination}/json?"
           + urllib.parse.urlencode({
               "key": key, "traffic": "true",
               "travelMode": _TOMTOM_MODES[mode_key],
               "maxAlternatives": 0,
           }))
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ToolError("TomTom rejected the API key — check TOMTOM_API_KEY") from exc
        if exc.code == 429 or exc.code >= 500:
            raise TransientToolError(f"TomTom routing returned {exc.code}") from exc
        if exc.code == 400:
            raise ToolError(
                "TomTom found no drivable route — an endpoint name may have "
                "resolved to the wrong place; try adding the city/state to it"
            ) from exc
        raise ToolError(f"TomTom routing error {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach TomTom: {exc}") from exc
    routes = data.get("routes") or []
    if not routes:
        raise ToolError("no route found between those points — check the destination name")
    summary = (routes[0].get("summary") or {})
    now_s = int(summary.get("travelTimeInSeconds", 0))
    delay_s = int(summary.get("trafficDelayInSeconds", 0))
    freeflow_s = int(summary.get("noTrafficTravelTimeInSeconds", 0)) or (now_s - delay_s)
    delay_min = max(0, round((now_s - freeflow_s) / 60))
    return {
        "mode": mode_key,
        "distance_km": round(summary.get("lengthInMeters", 0) / 1000, 1),
        "duration_now_min": round(now_s / 60),
        "duration_normal_min": round(freeflow_s / 60),
        "traffic_delay_min": delay_min,
        "traffic": ("heavy" if delay_min >= 15 else
                    "moderate" if delay_min >= 5 else "light"),
    }


def _seconds(value) -> int:
    # Durations arrive as protobuf-JSON strings like "2508s".
    try:
        return int(float(str(value).rstrip("s")))
    except (TypeError, ValueError):
        return 0


def _endpoint_arg(args: dict, side: str) -> dict:
    lat, lon = args.get(f"{side}_latitude"), args.get(f"{side}_longitude")
    if lat is not None and lon is not None:
        return {"location": {"latLng": {"latitude": float(lat),
                                        "longitude": float(lon)}}}
    place = str(args.get(side, "") or "").strip()
    if place:
        return {"address": place}  # Google geocodes address strings itself
    raise ToolError(f"routes.eta needs an {side} — a place name, or lat/lon "
                    "from the user's shared pin")


def _eta(args: dict) -> dict:
    mode_key = str(args.get("mode", "drive") or "drive").strip().lower()
    if mode_key not in _MODES:
        raise ToolError(f"mode must be one of {sorted(_MODES)}")
    google, tomtom = _google_key(), _tomtom_key()
    if not google and not tomtom:
        raise ToolError("travel times need GOOGLE_MAPS_API_KEY (Routes API) "
                        "or TOMTOM_API_KEY in .env")
    if google:
        try:
            return {**_google_eta(args, _MODES[mode_key], google), "source": "google"}
        except (ToolError, TransientToolError) as exc:
            if not tomtom:
                raise
            # Both backends carry LIVE traffic, so degrading is honest —
            # the source marks it for the audit trail (Google's Places
            # sibling hard-failed twice live before its OSM fallback).
            from kyraan.control_plane.logging_setup import log_event
            log_event("routes_tomtom_fallback", error=str(exc)[:200])
    return {**_tomtom_eta(args, mode_key, tomtom), "source": "tomtom" if not google else "tomtom-fallback"}


def _google_eta(args: dict, mode: str, key: str) -> dict:
    body = {
        "origin": _endpoint_arg(args, "origin"),
        "destination": _endpoint_arg(args, "destination"),
        "travelMode": mode,
    }
    if mode != "WALK":  # traffic applies to road vehicles only
        body["routingPreference"] = "TRAFFIC_AWARE"
    request = urllib.request.Request(
        _ENDPOINT, data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": "routes.duration,routes.staticDuration,"
                                "routes.distanceMeters,routes.description",
        }, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read()).get("error", {}).get("message", "")[:200]
        except Exception:
            pass
        if exc.code in (401, 403):
            raise ToolError(
                f"Google Routes refused the request ({exc.code}): "
                f"{detail or 'check that the Routes API is enabled and on the key restriction list'}"
            ) from exc
        if exc.code == 429:
            raise TransientToolError("Google Routes rate limit hit") from exc
        if exc.code >= 500:
            raise TransientToolError(f"Google Routes returned {exc.code}") from exc
        raise ToolError(f"Google Routes error {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Google Routes: {exc}") from exc

    routes = data.get("routes") or []
    if not routes:
        raise ToolError("no route found between those points — check the "
                        "destination name")
    route = routes[0]
    now_s = _seconds(route.get("duration"))
    freeflow_s = _seconds(route.get("staticDuration")) or now_s
    delay_min = max(0, round((now_s - freeflow_s) / 60))
    out = {
        "mode": mode.lower(),
        "distance_km": round(route.get("distanceMeters", 0) / 1000, 1),
        "duration_now_min": round(now_s / 60),
        "duration_normal_min": round(freeflow_s / 60),
        "traffic_delay_min": delay_min,
        "traffic": ("heavy" if delay_min >= 15 else
                    "moderate" if delay_min >= 5 else "light"),
    }
    if route.get("description"):
        out["via"] = route["description"]
    return out


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "routes.eta":
        return await asyncio.to_thread(_eta, args)
    raise ToolError(f"routes adapter does not provide {tool_name!r}")
