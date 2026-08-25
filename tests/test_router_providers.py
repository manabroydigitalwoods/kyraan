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


async def _noop():
    pass


def test_rate_limited_provider_enters_cooldown(monkeypatch):
    """Live degraded mode burned ~9 doomed HTTP calls per message before
    fallbacks engaged — after a rate-limit failure the provider is skipped
    outright for the cool-down window."""
    import pytest
    from kyraan.control_plane import config

    monkeypatch.setattr(router, "_cooldown_until", {})
    monkeypatch.setattr(router, "_RETRY_BACKOFF_SECONDS", [])
    attempts = []

    def limited(provider, model, prompt, system, max_tokens, force_json=False):
        attempts.append(1)
        raise RuntimeError("Error code: 429 - rate limit reached")

    monkeypatch.setattr(router, "_dispatch", limited)
    with pytest.raises(router.ModelProviderError):
        router.call(prompt="x", tier="frontier")
    assert len(attempts) == 1

    with pytest.raises(router.ModelProviderError, match="cooling down"):
        router.call(prompt="x", tier="frontier")
    assert len(attempts) == 1  # zero new HTTP attempts during cooldown

    # cooldown expiry re-enables the provider — whichever one the frontier
    # tier currently names (was hardcoded "groq"; broke when the tier moved
    # to openai).
    frontier_provider = config.load()["model_tiers"]["frontier"]["provider"]
    router._cooldown_until[frontier_provider] = 0.0
    with pytest.raises(router.ModelProviderError, match="failed after"):
        router.call(prompt="x", tier="frontier")
    assert len(attempts) == 2


def test_non_rate_errors_do_not_trigger_cooldown(monkeypatch):
    import pytest

    monkeypatch.setattr(router, "_cooldown_until", {})
    monkeypatch.setattr(router, "_RETRY_BACKOFF_SECONDS", [])

    def broken(provider, model, prompt, system, max_tokens, force_json=False):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(router, "_dispatch", broken)
    with pytest.raises(router.ModelProviderError):
        router.call(prompt="x", tier="frontier")
    assert router._cooldown_until == {}
