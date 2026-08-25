"""Home Assistant adapter — allowlist enforcement, API payloads, error
classification. All HTTP mocked."""
import io
import json
import urllib.request

import pytest

from kyraan.control_plane import config
from kyraan.tools import home_assistant, registry


@pytest.fixture
def hass_env(monkeypatch):
    monkeypatch.setenv("HASS_URL", "http://localhost:8123")
    monkeypatch.setenv("HASS_TOKEN", "tok")


@pytest.fixture
def fake_api(monkeypatch, hass_env):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append({"url": request.full_url, "method": request.get_method(),
                      "payload": json.loads(request.data) if request.data else None})
        return io.BytesIO(json.dumps(
            {"state": "off", "attributes": {"friendly_name": "AC", "unit_of_measurement": None}}
        ).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


async def test_get_state_reads_allowlisted_entity(fake_api):
    result = await home_assistant.call("home.get_state", {"entity": "switch.ac"})
    assert result == {"entity": "switch.ac", "state": "off", "unit": None, "name": "AC"}
    assert fake_api[0]["url"].endswith("/api/states/switch.ac")


async def test_unlisted_entity_is_refused_before_any_request(fake_api):
    with pytest.raises(registry.ToolError, match="not in Kyraan's allowlist"):
        await home_assistant.call("home.get_state", {"entity": "switch.ac_led"})
    assert fake_api == []


async def test_turn_off_posts_service_call_and_reads_back(fake_api):
    result = await home_assistant.call("home.turn_off", {"entity": "switch.ac"})
    assert fake_api[0]["url"].endswith("/api/services/switch/turn_off")
    assert fake_api[0]["payload"] == {"entity_id": "switch.ac"}
    assert fake_api[1]["url"].endswith("/api/states/switch.ac")  # read-back
    assert result["state"] == "off"


async def test_write_requires_write_allowlist_not_just_read(fake_api):
    """Read-allowlisted sensors must not be switchable."""
    with pytest.raises(registry.ToolError, match="not write-allowlisted"):
        await home_assistant.call("home.turn_on", {"entity": "sensor.ac_voltage"})
    assert fake_api == []


async def test_missing_setup_is_a_clear_instruction(monkeypatch):
    monkeypatch.delenv("HASS_URL", raising=False)
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    with pytest.raises(registry.ToolError, match="HASS_URL and HASS_TOKEN"):
        await home_assistant.call("home.get_state", {"entity": "switch.ac"})
