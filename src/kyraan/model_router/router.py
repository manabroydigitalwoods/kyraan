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
class Usage:
    """Token counts, best-effort — SDKs disagree on field names/availability,
    so any field can come back None rather than raising."""

    input_tokens: int | None
    output_tokens: int | None


@dataclass
class RoutedResponse:
    text: str
    tier_used: str
    provider: str
    model: str
    latency_ms: float
    usage: Usage
    reasoning: str | None = None


@dataclass
class _RawResult:
    text: str
    usage: Usage
    reasoning: str | None = None


def _call_anthropic(provider_cfg: dict, model: str, prompt: str, system: str, max_tokens: int) -> _RawResult:
    response = _get_anthropic_client(provider_cfg).messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system or None,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    # Extended thinking (when enabled) comes back as separate "thinking" blocks.
    thinking = "".join(block.thinking for block in response.content if block.type == "thinking")
    usage = Usage(
        input_tokens=getattr(response.usage, "input_tokens", None),
        output_tokens=getattr(response.usage, "output_tokens", None),
    )
    return _RawResult(text=text, usage=usage, reasoning=thinking or None)


def _call_gemini(provider_cfg: dict, model: str, prompt: str, system: str, max_tokens: int) -> _RawResult:
    from google.genai import types

    response = _get_gemini_client(provider_cfg).models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_tokens,
        ),
    )
    meta = getattr(response, "usage_metadata", None)
    usage = Usage(
        input_tokens=getattr(meta, "prompt_token_count", None) if meta else None,
        output_tokens=getattr(meta, "candidates_token_count", None) if meta else None,
    )
    return _RawResult(text=response.text or "", usage=usage)


def _call_openai_compatible(
    provider: str, provider_cfg: dict, model: str, prompt: str, system: str, max_tokens: int
) -> _RawResult:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # Most OpenAI-compatible gateways (Groq, OpenRouter, OpenCode, Ollama)
    # accept the traditional `max_tokens`, but native OpenAI's gpt-5 family
    # renamed it to `max_completion_tokens` and rejects the old name — a
    # provider can declare which one it wants via max_tokens_param.
    token_param = provider_cfg.get("max_tokens_param", "max_tokens")
    response = _get_openai_compatible_client(provider, provider_cfg).chat.completions.create(
        model=model,
        messages=messages,
        **{token_param: max_tokens},
    )
    usage_obj = getattr(response, "usage", None)
    usage = Usage(
        input_tokens=getattr(usage_obj, "prompt_tokens", None) if usage_obj else None,
        output_tokens=getattr(usage_obj, "completion_tokens", None) if usage_obj else None,
    )
    # "Reasoning" models (Groq's, OpenRouter's free tier, OpenAI's gpt-5
    # family) put hidden chain-of-thought here, separate from .content.
    reasoning = getattr(response.choices[0].message, "reasoning", None)
    return _RawResult(text=response.choices[0].message.content or "", usage=usage, reasoning=reasoning)


def _dispatch(provider: str, model: str, prompt: str, system: str, max_tokens: int) -> _RawResult:
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

# The most recent successful call's full RoutedResponse — a single-user,
# single-threaded CLI/TUI convenience for displaying "what just answered
# that, how long did it take, how many tokens" without threading metadata
# through every intermediate function's return type. Not meant as a public
# API for concurrent use.
last_call: RoutedResponse | None = None


def call(
    prompt: str,
    system: str = "",
    tier: str = "cheap",
    max_tokens: int = 1024,
) -> RoutedResponse:
    global last_call
    tier_cfg = config.load()["model_tiers"][tier]
    model = tier_cfg["model"]
    provider = tier_cfg["provider"]

    last_exc: Exception | None = None
    attempts = len(_RETRY_BACKOFF_SECONDS) + 1
    for attempt in range(attempts):
        start = time.monotonic()
        try:
            raw = _dispatch(provider, model, prompt, system, max_tokens)
            latency_ms = (time.monotonic() - start) * 1000
            log_event(
                "model_call",
                tier=tier,
                provider=provider,
                model=model,
                prompt_chars=len(prompt),
                attempt=attempt,
                latency_ms=round(latency_ms),
                input_tokens=raw.usage.input_tokens,
                output_tokens=raw.usage.output_tokens,
                had_reasoning=raw.reasoning is not None,
            )
            response = RoutedResponse(
                text=raw.text,
                tier_used=tier,
                provider=provider,
                model=model,
                latency_ms=latency_ms,
                usage=raw.usage,
                reasoning=raw.reasoning,
            )
            last_call = response
            return response
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
