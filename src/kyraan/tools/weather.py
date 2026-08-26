"""Weather adapter — tool #5 (2026-08-26). Open-Meteo: free, no API key,
no signup, structured current conditions + daily forecast straight from
coordinates. Built after the live soak showed weather-by-web-search is
inherently flaky: the model stuffed raw coordinates into five search
queries in a row (search engines match none of them), burned its step
cap, and then glossed a 10-day-forecast snippet as "currently sunny" at
8 PM. A location pin gives exact lat/lon — this answers from them
deterministically.

Accepts either a place name (geocoded via Open-Meteo's own geocoder) or
explicit latitude/longitude (from a shared Telegram pin).

Adapter contract (docs/design/tool_registry.md): `async def call(tool_name, args)`.
"""
import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request

from kyraan.tools.registry import ToolError, TransientToolError

_FORECAST = "https://api.open-meteo.com/v1/forecast"
_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"

# WMO weather interpretation codes → words (Open-Meteo's documented set).
_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "icy fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def _fetch(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise TransientToolError("Open-Meteo rate limit hit") from exc
        if exc.code >= 500:
            raise TransientToolError(f"Open-Meteo returned {exc.code}") from exc
        raise ToolError(f"Open-Meteo error {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientToolError(f"could not reach Open-Meteo: {exc}") from exc


def _geocode(place: str) -> tuple[float, float, str]:
    params = urllib.parse.urlencode({"name": place, "count": 1, "format": "json"})
    data = _fetch(f"{_GEOCODE}?{params}")
    results = data.get("results") or []
    if not results:
        raise ToolError(f"no place named {place!r} found — try a nearby town or district name")
    hit = results[0]
    label = ", ".join(p for p in (hit.get("name"), hit.get("admin1")) if p)
    return float(hit["latitude"]), float(hit["longitude"]), label


def _words(code) -> str:
    try:
        return _CODES.get(int(code), "")
    except (TypeError, ValueError):
        return ""


def _get(args: dict) -> dict:
    place = str(args.get("place", "") or "").strip()
    lat, lon = args.get("latitude"), args.get("longitude")
    if lat is not None and lon is not None:
        lat, lon = float(lat), float(lon)
        label = place  # a pin's place name, if the model passed it along
    elif place:
        lat, lon, label = _geocode(place)
    else:
        raise ToolError("weather.get needs either place or latitude+longitude")

    params = urllib.parse.urlencode({
        "latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,weather_code",
        "timezone": "auto", "forecast_days": 3,
    })
    data = _fetch(f"{_FORECAST}?{params}")

    current = data.get("current") or {}
    daily = data.get("daily") or {}
    days = []
    for i, date in enumerate((daily.get("time") or [])[:3]):
        days.append({
            "date": date,
            "sky": _words((daily.get("weather_code") or [None] * 3)[i]),
            "min_c": (daily.get("temperature_2m_min") or [None] * 3)[i],
            "max_c": (daily.get("temperature_2m_max") or [None] * 3)[i],
            "rain_chance_pct": (daily.get("precipitation_probability_max") or [None] * 3)[i],
        })
    return {
        **({"place": label} if label else {}),
        "coordinates": f"{lat:.4f}, {lon:.4f}",
        "now": {
            "sky": _words(current.get("weather_code")),
            "temp_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "humidity_pct": current.get("relative_humidity_2m"),
            "wind_kmh": current.get("wind_speed_10m"),
        },
        "daily_forecast": days,
    }


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "weather.get":
        return await asyncio.to_thread(_get, args)
    raise ToolError(f"weather adapter does not provide {tool_name!r}")
