"""Two-tier model routing: cheap by default, escalate to frontier on request
or when the cheap tier reports low confidence.

Each tier picks a provider in config/permissions.yaml's `provider` field:
  - anthropic: native Anthropic API (ANTHROPIC_API_KEY)
  - opencode: OpenCode Zen's OpenAI-compatible gateway (OPENCODE_API_KEY) —
    a stand-in while there's no Anthropic key; its free models are used by
    default (see permissions.yaml)
  - openai: native OpenAI API (OPENAI_API_KEY)
  - ollama: a local Ollama server's OpenAI-compatible endpoint, no key needed

Swapping a tier's `provider`/`model` in permissions.yaml is the only change
needed to move a tier between these — nothing else in the codebase changes.
"""
import os
from dataclasses import dataclass

from kyraan.control_plane import config
from kyraan.control_plane.logging_setup import log_event

# provider -> (base_url or None for the SDK default, api_key env var or None if unneeded)
_OPENAI_COMPATIBLE = {
    "opencode": ("https://opencode.ai/zen/v1", "OPENCODE_API_KEY"),
    "openai": (None, "OPENAI_API_KEY"),
    "ollama": (os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"), None),
}

_anthropic_client = None
_openai_compatible_clients: dict[str, object] = {}


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        _anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client


def _get_openai_compatible_client(provider: str):
    if provider not in _openai_compatible_clients:
        from openai import OpenAI

        base_url, api_key_env = _OPENAI_COMPATIBLE[provider]
        api_key = os.environ[api_key_env] if api_key_env else "not-needed"
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


def _call_anthropic(model: str, prompt: str, system: str, max_tokens: int) -> str:
    response = _get_anthropic_client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system or None,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def _call_openai_compatible(provider: str, model: str, prompt: str, system: str, max_tokens: int) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = _get_openai_compatible_client(provider).chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    return response.choices[0].message.content or ""


def _dispatch(provider: str, model: str, prompt: str, system: str, max_tokens: int) -> str:
    if provider == "anthropic":
        return _call_anthropic(model, prompt, system, max_tokens)
    if provider in _OPENAI_COMPATIBLE:
        return _call_openai_compatible(provider, model, prompt, system, max_tokens)
    raise ValueError(f"Unknown model provider: {provider!r}")


def call(
    prompt: str,
    system: str = "",
    tier: str = "cheap",
    max_tokens: int = 1024,
) -> RoutedResponse:
    tier_cfg = config.load()["model_tiers"][tier]
    model = tier_cfg["model"]
    provider = tier_cfg.get("provider", "anthropic")

    try:
        text = _dispatch(provider, model, prompt, system, max_tokens)
    except Exception as exc:
        log_event("model_call_error", tier=tier, provider=provider, model=model, error=str(exc))
        raise ModelProviderError(f"{provider}/{model} failed: {exc}") from exc

    log_event("model_call", tier=tier, provider=provider, model=model, prompt_chars=len(prompt))
    return RoutedResponse(text=text, tier_used=tier, model=model)


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
