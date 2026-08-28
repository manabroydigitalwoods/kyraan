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


def test_ollama_native_payload_sets_context_think_and_json(monkeypatch):
    """The native path exists precisely for think:false and a real context
    window — Ollama's ~4K default silently truncated the qa prompt and
    produced garbage loop replies (live 2026-08-26)."""
    import io
    import json
    import urllib.request

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        return io.BytesIO(json.dumps({
            "message": {"content": "{\"ok\": true}", "thinking": ""},
            "prompt_eval_count": 5, "eval_count": 3,
        }).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = router._call_ollama_native(
        {"think": False, "context_length": 16384},
        "qwen3:8b", "hi", "sys", 512, force_json=True)

    assert captured["url"].endswith("/api/chat")
    assert captured["payload"]["options"] == {"num_predict": 512, "num_ctx": 16384}
    assert captured["payload"]["think"] is False
    assert captured["payload"]["format"] == "json"
    assert result.text == "{\"ok\": true}"
    assert result.usage.input_tokens == 5 and result.usage.output_tokens == 3


def test_auth_errors_never_retry(monkeypatch):
    """A 401 is permanent for this process: it got 3 full attempts live
    (2026-08-27, bad key at boot), delaying the local fallback on every
    message. Auth failures break out of the retry loop immediately."""
    import pytest

    monkeypatch.setattr(router, "_cooldown_until", {})
    monkeypatch.setattr(router, "_RETRY_BACKOFF_SECONDS", [0, 0])
    attempts = []

    def unauthorized(provider, model, prompt, system, max_tokens, force_json=False):
        attempts.append(1)
        raise RuntimeError("Error code: 401 - {'error': {'message': "
                           "'Incorrect API key provided: sk-inval***'}}")

    monkeypatch.setattr(router, "_dispatch", unauthorized)
    with pytest.raises(router.ModelProviderError):
        router.call(prompt="x", tier="frontier")
    assert len(attempts) == 1


def test_credit_exhaustion_never_retries_and_cools_down(monkeypatch):
    """Live 2026-08-28: the account balance hit zero and every call
    burned 3 retries while the degradation stayed silent for ~40 min.
    insufficient_quota fails FAST (money doesn't appear between
    attempts), cools the provider, and logs its own kind so the health
    layer alerts the owner with the actual fix."""
    import json as _json

    import pytest
    from kyraan.control_plane import logging_setup

    monkeypatch.setattr(router, "_cooldown_until", {})
    monkeypatch.setattr(router, "_RETRY_BACKOFF_SECONDS", [0, 0])
    attempts = []

    def broke(provider, model, prompt, system, max_tokens, force_json=False):
        attempts.append(1)
        raise RuntimeError(
            "Error code: 429 - {'error': {'code': 'credit_balance_exhausted',"
            " 'type': 'insufficient_quota'}}")

    monkeypatch.setattr(router, "_dispatch", broke)
    with pytest.raises(router.ModelProviderError):
        router.call(prompt="x", tier="frontier")
    assert len(attempts) == 1                      # zero retries
    assert router._cooldown_until                  # provider cooling down
    events = [_json.loads(l) for l in
              logging_setup.EVENT_LOG.read_text().splitlines()]
    assert any(e["kind"] == "provider_credits_exhausted" for e in events)
    from kyraan.triggers import health_alerts
    assert "provider_credits_exhausted" in health_alerts.CRITICAL
