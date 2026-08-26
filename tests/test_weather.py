"""Weather tool — Open-Meteo adapter (geocode + forecast parsing, error
mapping) and its place in the agent loop's menu."""
import io
import json
import urllib.error

import pytest

from kyraan.agents import agent_loop
from kyraan.tools import registry as reg
from kyraan.tools import weather


def _forecast_payload():
    return {
        "current": {"temperature_2m": 28.4, "apparent_temperature": 33.1,
                    "relative_humidity_2m": 89, "weather_code": 2,
                    "wind_speed_10m": 6.2},
        "daily": {
            "time": ["2026-08-26", "2026-08-27", "2026-08-28"],
            "temperature_2m_max": [32.1, 31.0, 30.2],
            "temperature_2m_min": [26.0, 25.5, 25.1],
            "precipitation_probability_max": [40, 85, 90],
            "weather_code": [2, 63, 95],
        },
    }


def _geocode_payload():
    return {"results": [{"name": "Jalpaiguri", "admin1": "West Bengal",
                         "latitude": 26.516, "longitude": 88.726}]}


@pytest.fixture
def fake_meteo(monkeypatch):
    """Route urllib to canned Open-Meteo responses keyed by endpoint."""
    seen = {"urls": []}

    def install(geocode=None, forecast=None, error_code=None):
        def fake_urlopen(request, timeout=0):
            url = request.full_url
            seen["urls"].append(url)
            if error_code is not None:
                raise urllib.error.HTTPError(url, error_code, "err", {}, io.BytesIO(b"{}"))
            payload = geocode if "geocoding-api" in url else forecast
            class _Resp:
                def read(self):
                    return json.dumps(payload).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return _Resp()

        monkeypatch.setattr(weather.urllib.request, "urlopen", fake_urlopen)
        return seen

    return install


async def test_pin_coordinates_answer_directly(fake_meteo):
    seen = fake_meteo(forecast=_forecast_payload())
    result = await weather.call("weather.get", {
        "latitude": 26.65388, "longitude": 88.47222, "place": "Radhabari, Rajganj"})
    assert len(seen["urls"]) == 1                      # no geocoding round-trip
    assert "api.open-meteo.com" in seen["urls"][0]
    assert result["place"] == "Radhabari, Rajganj"
    assert result["now"]["temp_c"] == 28.4
    assert result["now"]["sky"] == "partly cloudy"
    assert result["daily_forecast"][1] == {
        "date": "2026-08-27", "sky": "rain", "min_c": 25.5,
        "max_c": 31.0, "rain_chance_pct": 85}


async def test_place_name_geocodes_first(fake_meteo):
    seen = fake_meteo(geocode=_geocode_payload(), forecast=_forecast_payload())
    result = await weather.call("weather.get", {"place": "Jalpaiguri"})
    assert "geocoding-api" in seen["urls"][0]
    assert result["place"] == "Jalpaiguri, West Bengal"
    assert result["coordinates"].startswith("26.516")


async def test_unknown_place_is_a_clear_error(fake_meteo):
    fake_meteo(geocode={"results": []})
    with pytest.raises(reg.ToolError, match="no place named"):
        await weather.call("weather.get", {"place": "Xyzzyville"})


async def test_no_args_refused(fake_meteo):
    fake_meteo(forecast=_forecast_payload())
    with pytest.raises(reg.ToolError, match="place or latitude"):
        await weather.call("weather.get", {})


async def test_error_mapping(fake_meteo):
    fake_meteo(error_code=429)
    with pytest.raises(reg.TransientToolError):
        await weather.call("weather.get", {"place": "Kolkata"})
    fake_meteo(error_code=503)
    with pytest.raises(reg.TransientToolError):
        await weather.call("weather.get", {"place": "Kolkata"})


def test_registry_entry_validates():
    spec = reg.get("weather.get")
    assert spec.permission == "auto"
    assert spec.side_effects == "read"


async def test_executor_normalizes_coordinates_for_the_dedup_rails(monkeypatch):
    """Live 2026-08-26: 88.47219 vs 88.4722 slipped past both repeat
    rails and one question cost three API calls — the executor rounds to
    4 decimals so reworded retries become byte-identical."""
    seen = []

    async def fake_run_tool(call):
        seen.append(call.args)
        return {"now": {}}

    monkeypatch.setattr(agent_loop.kernel, "run_tool", fake_run_tool)
    await agent_loop._weather_get(9, {"latitude": 26.65390, "longitude": 88.47219,
                                      "place": "Radhabari"}, "")
    await agent_loop._weather_get(9, {"latitude": 26.6539, "longitude": 88.4722,
                                      "place": "Radhabari"}, "")
    assert seen[0] == seen[1] == {"place": "Radhabari",
                                  "latitude": 26.6539, "longitude": 88.4722}


def test_menu_lists_weather_and_steers_search_away():
    block = agent_loop._tools_block()
    assert "weather.get" in block
    assert "never web.search" in block
    assert "weather.get" in agent_loop._READ_ONLY_TOOLS
