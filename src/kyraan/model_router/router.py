"""Two-tier model routing: cheap by default, escalate to frontier on request
or when the cheap tier reports low confidence.

Providers are a registry in config/permissions.yaml's `providers` section —
each entry has a `kind` (anthropic | gemini | openai_compatible) plus
whatever connection info that kind needs (api_key_env, base_url). Adding a
new OpenAI-compatible provider (another gateway, another local server) is a
config-only change; no code here needs to know about it. A tier just names
a `provider` + `model`; swapping either in permissions.yaml is the only
change needed to move a tier between providers.
"""
import os
import time
from dataclasses import dataclass

from kyraan.control_plane import config
from kyraan.control_plane.logging_setup import log_event

_anthropic_client = None
_gemini_client = None
_openai_compatible_clients: dict[str, object] = {}


def _provider_cfg(provider: str) -> dict:
    providers = config.load()["providers"]
    if provider not in providers:
        raise ValueError(f"Unknown model provider {provider!r} — add it to config/permissions.yaml's providers section")
    return providers[provider]


def _get_anthropic_client(provider_cfg: dict):
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        _anthropic_client = Anthropic(api_key=os.environ[provider_cfg["api_key_env"]])
    return _anthropic_client


def _get_gemini_client(provider_cfg: dict):
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        _gemini_client = genai.Client(api_key=os.environ[provider_cfg["api_key_env"]])
    return _gemini_client


def _get_openai_compatible_client(provider: str, provider_cfg: dict):
    if provider not in _openai_compatible_clients:
        from openai import OpenAI

        api_key_env = provider_cfg.get("api_key_env")
        api_key = os.environ[api_key_env] if api_key_env else "not-needed"
        base_url = provider_cfg.get("base_url")
        if provider == "ollama":
            base_url = os.environ.get("OLLAMA_BASE_URL") or base_url
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        _openai_compatible_clients[provider] = OpenAI(**kwargs)
    return _openai_compatible_clients[provider]


class ModelProviderError(Exception):
    """Wraps any provider SDK error (rate limit, outage, bad key, ...) into
    one type so callers can distinguish "the provider failed" from "the
    model responded but wasn't confident/parseable"."""


@dataclass
class RoutedResponse:
    text: str
    tier_used: str
    model: str


def _call_anthropic(provider_cfg: dict, model: str, prompt: str, system: str, max_tokens: int) -> str:
    response = _get_anthropic_client(provider_cfg).messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system or None,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def _call_gemini(provider_cfg: dict, model: str, prompt: str, system: str, max_tokens: int) -> str:
    from google.genai import types

    response = _get_gemini_client(provider_cfg).models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_tokens,
        ),
    )
    return response.text or ""


def _call_openai_compatible(provider: str, provider_cfg: dict, model: str, prompt: str, system: str, max_tokens: int) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = _get_openai_compatible_client(provider, provider_cfg).chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    return response.choices[0].message.content or ""


def _dispatch(provider: str, model: str, prompt: str, system: str, max_tokens: int) -> str:
    provider_cfg = _provider_cfg(provider)
    kind = provider_cfg["kind"]
    if kind == "anthropic":
        return _call_anthropic(provider_cfg, model, prompt, system, max_tokens)
    if kind == "gemini":
        return _call_gemini(provider_cfg, model, prompt, system, max_tokens)
    if kind == "openai_compatible":
        return _call_openai_compatible(provider, provider_cfg, model, prompt, system, max_tokens)
    raise ValueError(f"Unknown provider kind {kind!r} for provider {provider!r}")


_RETRY_BACKOFF_SECONDS = (0.5, 1.5)  # transient errors (rate limits, 503s) are common; retry before giving up


def call(
    prompt: str,
    system: str = "",
    tier: str = "cheap",
    max_tokens: int = 1024,
) -> RoutedResponse:
    tier_cfg = config.load()["model_tiers"][tier]
    model = tier_cfg["model"]
    provider = tier_cfg["provider"]

    last_exc: Exception | None = None
    attempts = len(_RETRY_BACKOFF_SECONDS) + 1
    for attempt in range(attempts):
        try:
            text = _dispatch(provider, model, prompt, system, max_tokens)
            log_event("model_call", tier=tier, provider=provider, model=model, prompt_chars=len(prompt), attempt=attempt)
            return RoutedResponse(text=text, tier_used=tier, model=model)
        except Exception as exc:
            last_exc = exc
            log_event("model_call_error", tier=tier, provider=provider, model=model, attempt=attempt, error=str(exc))
            if attempt < attempts - 1:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])

    raise ModelProviderError(f"{provider}/{model} failed after {attempts} attempts: {last_exc}") from last_exc


def call_with_escalation(
    prompt: str,
    system: str = "",
    confidence_floor: float = 0.6,
    max_tokens: int = 1024,
) -> RoutedResponse:
    """Try the cheap tier; escalate to frontier if the cheap tier can't do it.

    Escalation trigger is intentionally simple for Phase 1: the cheap-tier
    call fails, or the caller already knows the task is hard and passes
    tier='frontier' directly via `call()` instead of this helper.
    """
    try:
        return call(prompt, system=system, tier="cheap", max_tokens=max_tokens)
    except Exception as exc:
        log_event("model_escalation", reason=str(exc))
        return call(prompt, system=system, tier="frontier", max_tokens=max_tokens)
