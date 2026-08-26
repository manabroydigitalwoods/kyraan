"""Location pins — Nominatim parsing, the coordinates fallback, and the
Telegram handler feeding the described place into the normal pipeline."""
import json
import urllib.error
from types import SimpleNamespace

import pytest

from kyraan.channels import location


def _nominatim_payload():
    return {
        "display_name": "Gajoldoba, Jalpaiguri, West Bengal, India",
        "address": {
            "village": "Gajoldoba",
            "state_district": "Jalpaiguri",
            "state": "West Bengal",
            "country": "India",
        },
    }


def test_describe_composes_place_and_coordinates(monkeypatch):
    def fake_urlopen(request, timeout=0):
        assert "nominatim.openstreetmap.org" in request.full_url
        assert request.headers.get("User-agent")  # OSM policy: real UA
        class _Resp:
            def read(self):
                return json.dumps(_nominatim_payload()).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return _Resp()

    monkeypatch.setattr(location.urllib.request, "urlopen", fake_urlopen)
    out = location.describe(26.75731, 88.59243)
    assert out == "Gajoldoba, Jalpaiguri, West Bengal (26.75731, 88.59243)"


def test_describe_falls_back_to_coordinates_on_geocoder_failure(monkeypatch):
    def refuse(request, timeout=0):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(location.urllib.request, "urlopen", refuse)
    out = location.describe(26.75731, 88.59243)
    assert out == "26.75731, 88.59243"  # never dropped, never an exception


async def test_location_pin_flows_into_the_pipeline(monkeypatch):
    """The live 2026-08-26 gap: a shared pin matched no handler and was
    silently dropped. Now it becomes a bracketed text message through the
    same _ingest path as typed text."""
    from kyraan.channels import telegram_bot

    monkeypatch.setenv("TELEGRAM_OWNER_ID", "1")
    monkeypatch.setattr(location, "describe",
                        lambda lat, lon: f"Testville ({lat:.5f}, {lon:.5f})")
    ingested = []

    async def fake_ingest(update, context, text):
        ingested.append(text)

    monkeypatch.setattr(telegram_bot, "_ingest", fake_ingest)

    async def send_chat_action(**kwargs):
        pass

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=9, type="private"),
        message=SimpleNamespace(location=SimpleNamespace(latitude=26.75731,
                                                         longitude=88.59243)),
    )
    ctx = SimpleNamespace(bot=SimpleNamespace(send_chat_action=send_chat_action))

    await telegram_bot._on_location(update, ctx)
    assert ingested == ["[I'm sharing my current location: "
                        "Testville (26.75731, 88.59243)]"]
