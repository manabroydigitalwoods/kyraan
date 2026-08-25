import pytest

from kyraan.model_router import router

_OLLAMA_CFG = {"kind": "openai_compatible", "base_url": "http://localhost:11434/v1"}


@pytest.fixture(autouse=True)
def _reset_client_cache():
    router._openai_compatible_clients.clear()
    yield
    router._openai_compatible_clients.clear()


def test_ollama_falls_back_to_configured_base_url_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    client = router._get_openai_compatible_client("ollama", _OLLAMA_CFG)
    assert str(client.base_url) == "http://localhost:11434/v1/"


def test_ollama_falls_back_to_configured_base_url_when_env_var_empty(monkeypatch):
    """Regression test: OLLAMA_BASE_URL="" (present but empty, e.g. an unset
    line in .env) used to be returned as-is by os.environ.get(key, default)
    since the key existed, silently disabling the base_url override and
    sending calls to OpenAI's real API instead of the local server."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    client = router._get_openai_compatible_client("ollama", _OLLAMA_CFG)
    assert str(client.base_url) == "http://localhost:11434/v1/"


def test_ollama_honors_a_real_env_var_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example.internal:11434/v1")
    client = router._get_openai_compatible_client("ollama", _OLLAMA_CFG)
    assert str(client.base_url) == "http://example.internal:11434/v1/"
