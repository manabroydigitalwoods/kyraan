"""Travel times — Google Routes adapter (endpoint forms, duration math,
error mapping, no-fallback honesty) and the agent-loop wiring."""
import io
import json
import urllib.error

import pytest

from kyraan.agents import agent_loop
from kyraan.tools import registry as reg
from kyraan.tools import routes


def _routes_payload():
    return {"routes": [{
        "duration": "2520s", "staticDuration": "1800s",
        "distanceMeters": 42300, "description": "NH27",
    }]}


@pytest.fixture
def fake_routes(monkeypatch):
    seen = {}

    def install(payload=None, error_code=None, error_message=""):
        def fake_urlopen(request, timeout=0):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data)
            seen["mask"] = request.headers.get("X-goog-fieldmask")
            if error_code is not None:
                err = json.dumps({"error": {"message": error_message}}).encode()
                raise urllib.error.HTTPError(
                    request.full_url, error_code, "err", {}, io.BytesIO(err))
            class _Resp:
                def read(self):
                    return json.dumps(payload).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return _Resp()

        monkeypatch.setattr(routes.urllib.request, "urlopen", fake_urlopen)
        return seen

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "gkey")
    monkeypatch.delenv("TOMTOM_API_KEY", raising=False)
    return install


async def test_pin_to_place_with_traffic_delta(fake_routes):
    seen = fake_routes(payload=_routes_payload())
    result = await routes.call("routes.eta", {
        "origin_latitude": 26.6539, "origin_longitude": 88.4722,
        "destination": "Jalpaiguri"})
    assert seen["body"]["origin"]["location"]["latLng"]["latitude"] == 26.6539
    assert seen["body"]["destination"] == {"address": "Jalpaiguri"}
    assert seen["body"]["routingPreference"] == "TRAFFIC_AWARE"
    assert result == {"mode": "drive", "distance_km": 42.3,
                      "duration_now_min": 42, "duration_normal_min": 30,
                      "traffic_delay_min": 12, "traffic": "moderate",
                      "via": "NH27", "source": "google"}


async def test_place_to_place_distance_question(fake_routes):
    """'What is the distance between Kolkata and Jalpaiguri' — both ends
    as names, Google geocodes them itself."""
    seen = fake_routes(payload=_routes_payload())
    result = await routes.call("routes.eta", {
        "origin": "Kolkata", "destination": "Jalpaiguri"})
    assert seen["body"]["origin"] == {"address": "Kolkata"}
    assert result["distance_km"] == 42.3


async def test_walk_mode_skips_traffic_preference(fake_routes):
    seen = fake_routes(payload=_routes_payload())
    await routes.call("routes.eta", {
        "origin": "A", "destination": "B", "mode": "walk"})
    assert seen["body"]["travelMode"] == "WALK"
    assert "routingPreference" not in seen["body"]


async def test_missing_endpoint_refused(fake_routes):
    fake_routes(payload=_routes_payload())
    with pytest.raises(reg.ToolError, match="destination"):
        await routes.call("routes.eta", {"origin": "Kolkata"})


async def test_no_key_is_a_clear_config_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("TOMTOM_API_KEY", raising=False)
    with pytest.raises(reg.ToolError, match="GOOGLE_MAPS_API_KEY"):
        await routes.call("routes.eta", {"origin": "A", "destination": "B"})
    assert not routes.configured()


async def test_403_carries_googles_own_reason(fake_routes):
    fake_routes(error_code=403, error_message="Routes API has not been used")
    with pytest.raises(reg.ToolError, match="Routes API has not been used"):
        await routes.call("routes.eta", {"origin": "A", "destination": "B"})


async def test_5xx_is_transient(fake_routes):
    fake_routes(error_code=503)
    with pytest.raises(reg.TransientToolError):
        await routes.call("routes.eta", {"origin": "A", "destination": "B"})


def test_registry_and_menu(monkeypatch):
    spec = reg.get("routes.eta")
    assert spec.permission == "auto" and spec.side_effects == "read"
    monkeypatch.delenv("TOMTOM_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    assert "- routes.eta" in agent_loop._tools_block()
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    assert "- routes.eta" not in agent_loop._tools_block()
    monkeypatch.setenv("TOMTOM_API_KEY", "t")   # TomTom alone also lights it up
    assert "- routes.eta" in agent_loop._tools_block()
    assert "routes.eta" in agent_loop._READ_ONLY_TOOLS


async def test_google_failure_falls_back_to_tomtom(monkeypatch):
    """Both backends carry LIVE traffic, so degrading is honest — a
    Google error with a TomTom key present answers from TomTom, marked
    in source for the audit trail."""
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "gkey")
    monkeypatch.setenv("TOMTOM_API_KEY", "tkey")

    def google_down(args, mode, key):
        raise routes.ToolError("routes api disabled")

    def tomtom_answers(args, mode_key, key):
        return {"mode": mode_key, "distance_km": 42.3, "duration_now_min": 42,
                "duration_normal_min": 30, "traffic_delay_min": 12,
                "traffic": "moderate"}

    monkeypatch.setattr(routes, "_google_eta", google_down)
    monkeypatch.setattr(routes, "_tomtom_eta", tomtom_answers)
    result = await routes.call("routes.eta", {"origin": "A", "destination": "B"})
    assert result["source"] == "tomtom-fallback"
    assert result["duration_now_min"] == 42


async def test_tomtom_alone_is_primary(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.setenv("TOMTOM_API_KEY", "tkey")

    def tomtom_answers(args, mode_key, key):
        return {"mode": mode_key, "distance_km": 1.0, "duration_now_min": 5,
                "duration_normal_min": 5, "traffic_delay_min": 0,
                "traffic": "light"}

    monkeypatch.setattr(routes, "_tomtom_eta", tomtom_answers)
    result = await routes.call("routes.eta", {"origin": "A", "destination": "B"})
    assert result["source"] == "tomtom"


async def test_tomtom_summary_parsing(monkeypatch):
    """TomTom's shapes: geocode positions, then routing summary seconds."""
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.setenv("TOMTOM_API_KEY", "tkey")
    import io as _io, json as _json, urllib.error as _ue

    def fake_urlopen(request, timeout=0):
        url = request.full_url
        if "search/2/search" in url:
            payload = {"results": [{"position": {"lat": 26.7, "lon": 88.4}}]}
        else:
            assert "traffic=true" in url and "travelMode=car" in url
            payload = {"routes": [{"summary": {
                "lengthInMeters": 48500, "travelTimeInSeconds": 5880,
                "trafficDelayInSeconds": 720,
                "noTrafficTravelTimeInSeconds": 5160}}]}
        class _Resp:
            def read(self):
                return _json.dumps(payload).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return _Resp()

    monkeypatch.setattr(routes.urllib.request, "urlopen", fake_urlopen)
    result = await routes.call("routes.eta", {
        "origin": "City Center Mall, Siliguri", "destination": "Jalpaiguri"})
    assert result == {"mode": "drive", "distance_km": 48.5,
                      "duration_now_min": 98, "duration_normal_min": 86,
                      "traffic_delay_min": 12, "traffic": "moderate",
                      "source": "tomtom"}
