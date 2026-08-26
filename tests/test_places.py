"""Nearby places — Overpass parsing/sorting, the Google backend switch,
category validation, and the agent-loop wiring."""
import io
import json
import urllib.error

import pytest

from kyraan.agents import agent_loop
from kyraan.tools import places
from kyraan.tools import registry as reg


def _overpass_payload():
    return {"elements": [
        {"type": "node", "lat": 26.70, "lon": 88.47,
         "tags": {"name": "Far Hospital"}},
        {"type": "node", "lat": 26.66, "lon": 88.47,
         "tags": {"name": "Near Hospital", "phone": "+91 123",
                  "opening_hours": "24/7"}},
        {"type": "way", "center": {"lat": 26.68, "lon": 88.48},
         "tags": {"name": "Mid Clinic"}},
        {"type": "node", "lat": 26.655, "lon": 88.472, "tags": {}},  # unnamed
    ]}


def _google_payload():
    return {"places": [
        {"displayName": {"text": "Rated Hospital"},
         "location": {"latitude": 26.66, "longitude": 88.47},
         "rating": 4.4, "userRatingCount": 210,
         "currentOpeningHours": {"openNow": True}},
    ]}


@pytest.fixture
def fake_backend(monkeypatch):
    seen = {}

    def install(payload=None, error_code=None):
        def fake_urlopen(request, timeout=0):
            seen["url"] = request.full_url
            seen["headers"] = dict(request.headers)
            seen["body"] = request.data
            if error_code is not None:
                raise urllib.error.HTTPError(
                    request.full_url, error_code, "err", {}, io.BytesIO(b"{}"))
            class _Resp:
                def read(self):
                    return json.dumps(payload).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return _Resp()

        monkeypatch.setattr(places.urllib.request, "urlopen", fake_urlopen)
        return seen

    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    return install


async def test_overpass_parses_sorts_and_skips_unnamed(fake_backend):
    seen = fake_backend(payload=_overpass_payload())
    result = await places.call("places.nearby", {
        "category": "hospital", "latitude": 26.6539, "longitude": 88.4722})
    assert "overpass-api.de" in seen["url"]
    assert result["source"] == "openstreetmap"
    names = [r["name"] for r in result["results"]]
    assert names == ["Near Hospital", "Mid Clinic", "Far Hospital"]  # by distance
    first = result["results"][0]
    assert first["phone"] == "+91 123" and first["hours"] == "24/7"
    assert first["map"].startswith("https://www.google.com/maps/search/")
    assert first["distance_km"] < result["results"][1]["distance_km"]


async def test_google_backend_used_when_key_set(fake_backend, monkeypatch):
    seen = fake_backend(payload=_google_payload())
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "gkey")
    result = await places.call("places.nearby", {
        "category": "hospital", "latitude": 26.6539, "longitude": 88.4722})
    assert "places.googleapis.com" in seen["url"]
    assert seen["headers"].get("X-goog-api-key") == "gkey"
    assert result["source"] == "google"
    assert result["results"][0]["rating"] == "4.4 (210 reviews)"
    assert result["results"][0]["open_now"] is True


async def test_unknown_category_lists_the_valid_ones(fake_backend):
    fake_backend(payload=_overpass_payload())
    with pytest.raises(reg.ToolError, match="hospital.*sightseeing"):
        await places.call("places.nearby", {
            "category": "casino", "latitude": 1.0, "longitude": 1.0})


async def test_no_location_refused(fake_backend):
    fake_backend(payload=_overpass_payload())
    with pytest.raises(reg.ToolError, match="latitude"):
        await places.call("places.nearby", {"category": "atm"})


async def test_empty_results_widen_once_then_note(fake_backend, monkeypatch):
    """Rural pins (live: zero hospitals within 3 km of the owner's) get
    ONE automatic widen to 10 km before the honest sparse-data note."""
    radii = []
    real = places._overpass

    def spy(lat, lon, category, radius_m):
        radii.append(radius_m)
        return []

    monkeypatch.setattr(places, "_overpass", spy)
    result = await places.call("places.nearby", {
        "category": "hotel", "latitude": 26.6539, "longitude": 88.4722})
    assert radii == [3000, 10000]          # widened once, automatically
    assert result["radius_km"] == 10.0
    assert result["results"] == []
    assert "sparse" in result["note"]

    radii.clear()
    await places.call("places.nearby", {   # explicit radius: no auto-widen
        "category": "hotel", "latitude": 26.6539, "longitude": 88.4722,
        "radius_m": 2000})
    assert radii == [2000]


async def test_google_failure_falls_back_to_osm(fake_backend, monkeypatch):
    """Live 2026-08-26: a hospital lookup hard-failed twice while the
    Places API enable was propagating — with a key set, a Google error
    must degrade to the free OSM backend inside the same call."""
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "gkey")

    def google_down(lat, lon, category, radius_m, key):
        raise places.ToolError("google rejected")

    def osm_answers(lat, lon, category, radius_m):
        return [{"name": "HWC Balaram hat", "distance_km": 1.54,
                 "map": "https://maps.example"}]

    monkeypatch.setattr(places, "_google", google_down)
    monkeypatch.setattr(places, "_overpass", osm_answers)
    result = await places.call("places.nearby", {
        "category": "hospital", "latitude": 26.6539, "longitude": 88.4722})
    assert result["source"] == "openstreetmap-fallback"
    assert result["results"][0]["name"] == "HWC Balaram hat"


async def test_backend_errors_map_cleanly(fake_backend):
    fake_backend(error_code=429)
    with pytest.raises(reg.TransientToolError):
        await places.call("places.nearby", {
            "category": "atm", "latitude": 1.0, "longitude": 1.0})


def test_registry_and_menu():
    spec = reg.get("places.nearby")
    assert spec.permission == "auto" and spec.side_effects == "read"
    block = agent_loop._tools_block()
    assert "places.nearby" in block
    assert "places.nearby" in agent_loop._READ_ONLY_TOOLS
