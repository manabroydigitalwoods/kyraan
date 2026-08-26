"""Nearby-places adapter — tool #6 (2026-08-26). "Hospital near me",
"ATMs around here", restaurants/hotels/sightseeing by a shared pin or a
named place.

Two backends behind one tool, chosen by environment:
- Default: OpenStreetMap's Overpass API — free, keyless, fits the stack
  (SearXNG, Open-Meteo, Nominatim). Coverage in India is strong for
  hospitals/ATMs/fuel, patchier for restaurants/hotels, and has no
  ratings.
- GOOGLE_MAPS_API_KEY set: Google Places API (New) — better POI data,
  ratings, open-now; needs GCP billing enabled (card on file), which is
  why it's the opt-in and not the default.

Every result carries a Google Maps link (the universal maps URL needs no
key) so a tap in Telegram opens navigation.

Adapter contract (docs/design/tool_registry.md): `async def call(tool_name, args)`.
"""
import asyncio
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request

from kyraan.tools.registry import ToolError, TransientToolError

_OVERPASS = "https://overpass-api.de/api/interpreter"
_GOOGLE = "https://places.googleapis.com/v1/places:searchNearby"
_MAX_RESULTS = 8
_DEFAULT_RADIUS_M = 3000
_MAX_RADIUS_M = 15000

# category → (overpass selectors, google includedTypes)
_CATEGORIES = {
    "hospital": (['"amenity"="hospital"', '"amenity"="clinic"'], ["hospital"]),
    "pharmacy": (['"amenity"="pharmacy"'], ["pharmacy"]),
    "atm": (['"amenity"="atm"'], ["atm"]),
    "bank": (['"amenity"="bank"'], ["bank"]),
    "restaurant": (['"amenity"="restaurant"', '"amenity"="fast_food"'], ["restaurant"]),
    "cafe": (['"amenity"="cafe"'], ["cafe"]),
    "hotel": (['"tourism"="hotel"', '"tourism"="guest_house"'], ["lodging"]),
    "sightseeing": (['"tourism"="attraction"', '"tourism"="viewpoint"',
                     '"tourism"="museum"', '"historic"'], ["tourist_attraction"]),
    "fuel": (['"amenity"="fuel"'], ["gas_station"]),
    "police": (['"amenity"="police"'], ["police"]),
    "grocery": (['"shop"="supermarket"', '"shop"="convenience"'], ["supermarket"]),
}


def categories() -> list:
    return sorted(_CATEGORIES)


def _distance_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _maps_link(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


def _post(url: str, data: bytes, headers: dict) -> dict:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=12) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ToolError(
                "the places backend rejected the request — if GOOGLE_MAPS_API_KEY "
                "is set, check the key and that the Places API (New) is enabled"
            ) from exc
        if exc.code == 429:
            raise TransientToolError("places backend rate limit hit") from exc
        if exc.code >= 500:
            raise TransientToolError(f"places backend returned {exc.code}") from exc
        raise ToolError(f"places backend error {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach the places backend: {exc}") from exc


def _overpass(lat: float, lon: float, category: str, radius_m: int) -> list:
    selectors, _ = _CATEGORIES[category]
    # nwr = node|way|relation in one clause; qt = fastest output order.
    clauses = "".join(
        f'nwr[{s}](around:{radius_m},{lat:.6f},{lon:.6f});' for s in selectors
    )
    query = f"[out:json][timeout:10];({clauses});out center tags qt 40;"
    data = _post(_OVERPASS, urllib.parse.urlencode({"data": query}).encode(), {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        # Overpass's usage policy wants an identifying UA; without these
        # two headers the public instance answers 406. Under load it also
        # stalls transiently — the registry's retries are the recovery.
        "User-Agent": "Kyraan/1.0 (personal assistant; self-hosted)",
    })
    results = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("name:en") or ""
        plat = el.get("lat") or (el.get("center") or {}).get("lat")
        plon = el.get("lon") or (el.get("center") or {}).get("lon")
        if not name or plat is None or plon is None:
            continue  # unnamed OSM stubs aren't answers a person can use
        entry = {
            "name": name,
            "distance_km": round(_distance_km(lat, lon, plat, plon), 2),
            "map": _maps_link(plat, plon),
        }
        for tag, key in (("phone", "phone"), ("opening_hours", "hours")):
            if tags.get(tag):
                entry[key] = tags[tag]
        results.append(entry)
    results.sort(key=lambda e: e["distance_km"])
    return results[:_MAX_RESULTS]


def _google(lat: float, lon: float, category: str, radius_m: int, key: str) -> list:
    _, types = _CATEGORIES[category]
    body = json.dumps({
        "includedTypes": types,
        "maxResultCount": _MAX_RESULTS,
        "locationRestriction": {"circle": {
            "center": {"latitude": lat, "longitude": lon},
            "radius": float(radius_m),
        }},
        "rankPreference": "DISTANCE",
    }).encode()
    data = _post(_GOOGLE, body, {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "places.displayName,places.location,"
                            "places.rating,places.userRatingCount,"
                            "places.currentOpeningHours.openNow",
    })
    results = []
    for p in data.get("places", []):
        plat = (p.get("location") or {}).get("latitude")
        plon = (p.get("location") or {}).get("longitude")
        if plat is None or plon is None:
            continue
        entry = {
            "name": (p.get("displayName") or {}).get("text", ""),
            "distance_km": round(_distance_km(lat, lon, plat, plon), 2),
            "map": _maps_link(plat, plon),
        }
        if p.get("rating") is not None:
            entry["rating"] = f"{p['rating']} ({p.get('userRatingCount', 0)} reviews)"
        open_now = (p.get("currentOpeningHours") or {}).get("openNow")
        if open_now is not None:
            entry["open_now"] = open_now
        results.append(entry)
    return results


def _nearby(args: dict) -> dict:
    category = str(args.get("category", "")).strip().lower()
    if category not in _CATEGORIES:
        raise ToolError(f"category must be one of: {', '.join(categories())}")
    place_label = str(args.get("place", "") or "").strip()
    lat, lon = args.get("latitude"), args.get("longitude")
    if lat is None or lon is None:
        if not place_label:
            raise ToolError("places.nearby needs latitude+longitude (from the "
                            "user's pin) or a place name")
        from kyraan.tools.weather import _geocode  # same free geocoder
        lat, lon, place_label = _geocode(place_label)
    lat, lon = float(lat), float(lon)
    explicit_radius = bool(args.get("radius_m"))
    radius_m = min(int(args.get("radius_m", _DEFAULT_RADIUS_M) or _DEFAULT_RADIUS_M),
                   _MAX_RADIUS_M)

    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    backend = "google" if key else "openstreetmap"

    def fetch(r):
        nonlocal backend
        if key and backend == "google":
            try:
                return _google(lat, lon, category, r, key)
            except (ToolError, TransientToolError) as exc:
                # A configured Google backend must degrade, not fail: seen
                # live 2026-08-26 — a hospital lookup errored twice while
                # the Places API enable was still propagating, though the
                # free OSM backend had the answer. The source marks the
                # degradation for the audit trail.
                from kyraan.control_plane.logging_setup import log_event
                log_event("places_google_fallback", error=str(exc)[:200])
                backend = "openstreetmap-fallback"
        return _overpass(lat, lon, category, r)

    results = fetch(radius_m)
    if not results and not explicit_radius and radius_m < 10000:
        # Rural reality (live: zero hospitals mapped within 3 km of the
        # owner's pin): widen once ourselves rather than making the model
        # ask the user's permission to look a little further.
        radius_m = 10000
        results = fetch(radius_m)
    out = {
        "category": category,
        **({"near": place_label} if place_label else {}),
        "radius_km": round(radius_m / 1000, 1),
        "source": backend,
        "results": results,
    }
    if not results:
        out["note"] = (f"nothing mapped within {out['radius_km']} km — offer to "
                       "widen the radius or try another category; in rural areas "
                       "the map data may simply be sparse")
    return out


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "places.nearby":
        return await asyncio.to_thread(_nearby, args)
    raise ToolError(f"places adapter does not provide {tool_name!r}")
