"""Telegram location pins → text the orchestrator can use.

A shared pin arrives as coordinates, not words (seen live 2026-08-26: the
owner shared a pin, the bot had no handler, the pin was silently dropped
and the model kept asking "which area are you in?"). This module turns
lat/lon into a short place description via OpenStreetMap's Nominatim
reverse geocoder — free, no key, but their usage policy requires a real
User-Agent and at most 1 request/second (a person sharing a pin is far
below that).

Privacy: the coordinates go to nominatim.openstreetmap.org, a third
party — only ever for a pin the owner explicitly shared, which is the
consent. Geocoding is best-effort: on any failure the raw coordinates
are used instead, so a shared location never gets dropped again.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

_ENDPOINT = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "Kyraan personal assistant (self-hosted; contact: owner)"

# Most-specific-first pick from Nominatim's address parts — enough for
# "weather here" and "what's near me" without quoting a full postal line.
_PLACE_KEYS = ("village", "hamlet", "town", "suburb", "city_district", "city",
               "county", "state_district", "state")


def describe(lat: float, lon: float) -> str:
    """One line for the model: place name if the geocoder answers,
    coordinates either way (they stay the ground truth)."""
    coords = f"{lat:.5f}, {lon:.5f}"
    place = _reverse(lat, lon)
    return f"{place} ({coords})" if place else coords


def _reverse(lat: float, lon: float) -> str:
    params = urllib.parse.urlencode({
        "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
        "format": "jsonv2", "zoom": 14, "addressdetails": 1,
    })
    request = urllib.request.Request(
        f"{_ENDPOINT}?{params}",
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return ""
    address = data.get("address") or {}
    parts = []
    for key in _PLACE_KEYS:
        value = address.get(key)
        if value and value not in parts:
            parts.append(value)
        if len(parts) == 3:
            break
    return ", ".join(parts)
