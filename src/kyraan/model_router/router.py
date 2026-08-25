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
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from kyraan.control_plane import config
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.logging_setup import log_event

# Durable per-day spend, keyed by local date — so the daily budget cap
# can't be dodged by restarting the process (session_cost_usd resets,
# this doesn't). Plain JSON like every other Phase 1/2 store.
COST_LEDGER_PATH = Path(__file__).resolve().parents[3] / "data" / "cost_ledger.json"


def _read_ledger() -> dict:
    try:
        return json.loads(COST_LEDGER_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def today_cost_usd() -> float:
    return float(_read_ledger().get(local_now().date().isoformat(), 0.0))


def _record_cost(cost_usd: float) -> None:
    if cost_usd <= 0:
        return
    ledger = _read_ledger()
    key = local_now().date().isoformat()
    ledger[key] = round(ledger.get(key, 0.0) + cost_usd, 6)
    COST_LEDGER_PATH.parent.mkdir(exist_ok=True)
    COST_LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


def _provider_token_limit(provider: str) -> int:
    return int((config.load()["providers"].get(provider) or {}).get("daily_token_limit", 0))


def provider_tokens_today(provider: str) -> int:
    key = f"tokens:{provider}:{local_now().date().isoformat()}"
    return int(_read_ledger().get(key, 0))


def _record_tokens(provider: str, usage: "Usage") -> None:
    total = (usage.input_tokens or 0) + (usage.output_tokens or 0)
    if total <= 0:
        return
    ledger = _read_ledger()
    key = f"tokens:{provider}:{local_now().date().isoformat()}"
    ledger[key] = int(ledger.get(key, 0)) + total
    COST_LEDGER_PATH.parent.mkdir(exist_ok=True)
    COST_LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


def quota_alert_due() -> str:
    """Once per provider per day, the first time usage crosses 80% of a
    declared daily_token_limit — returns a human warning line, or "".
    The Groq free tier ran dry live with zero warning; never again."""
    ledger = _read_ledger()
    day = local_now().date().isoformat()
    for provider in config.load()["providers"]:
        limit = _provider_token_limit(provider)
        if limit <= 0:
            continue
        used = int(ledger.get(f"tokens:{provider}:{day}", 0))
        if used < 0.8 * limit:
            continue
        marker = f"quota_alerted:{provider}:{day}"
        if ledger.get(marker):
            continue
        ledger[marker] = True
        COST_LEDGER_PATH.write_text(json.dumps(ledger, indent=2))
        log_event("quota_alert", provider=provider, used=used, limit=limit)
        return (
            f"{provider} is at {used * 100 // limit}% of its {limit:,}-token free daily "
            "quota — replies may switch to the local model when it runs out"
        )
    return ""


def budget_alert_due() -> bool:
    """True exactly once per day, the first time today's spend crosses
    cost_monitor.alert_threshold_pct of the daily budget — the caller
    surfaces the warning to the user. Alerted-dates live in the ledger
    file so a restart doesn't re-alert."""
    budget = daily_budget_usd()
    if budget <= 0:
        return False
    spent = today_cost_usd()
    if (spent / budget) * 100 < budget_alert_threshold_pct():
        return False
    ledger = _read_ledger()
    key = local_now().date().isoformat()
    alerted = ledger.get("alerted_dates", [])
    if key in alerted:
        return False
    ledger["alerted_dates"] = alerted + [key]
    COST_LEDGER_PATH.parent.mkdir(exist_ok=True)
    COST_LEDGER_PATH.write_text(json.dumps(ledger, indent=2))
    log_event("budget_alert", spent_today=spent, budget=budget)
    return True

# Circuit breaker: after a rate-limit failure a provider is skipped for a
# cool-down instead of burning 3 retries per call site per message — in
# live degraded mode that was ~9 doomed HTTP calls (~12s) per message
# before the fallbacks engaged.
_COOLDOWN_S = 120
_cooldown_until: dict = {}

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


def strip_code_fence(text: str) -> str:
    """Local models sometimes wrap the JSON they were asked for in a
    markdown code fence (```json ... ```) — seen live from llama3.1:8b,
    where an otherwise-correct intent classification failed to parse.
    Every call site that json.loads() model output should pass it through
    here first. Anything that isn't fence-wrapped comes back unchanged."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    first_newline = stripped.find("\n")
    if first_newline == -1:
        return stripped
    body = stripped[first_newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


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
    cost_usd: float = 0.0


def _cost_usd(tier_cfg: dict, usage: Usage) -> float:
    """USD per config/permissions.yaml's `pricing` (USD per 1M tokens) on
    the tier — absent means free, matching our currently free-tier
    providers. Missing token counts count as 0 tokens for that side rather
    than raising, consistent with Usage's best-effort contract."""
    pricing = tier_cfg.get("pricing")
    if not pricing:
        return 0.0
    input_cost = (usage.input_tokens or 0) / 1_000_000 * pricing.get("input_per_million", 0)
    output_cost = (usage.output_tokens or 0) / 1_000_000 * pricing.get("output_per_million", 0)
    return input_cost + output_cost


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
    provider: str, provider_cfg: dict, model: str, prompt: str, system: str, max_tokens: int,
    force_json: bool = False,
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
    kwargs = {token_param: max_tokens}
    if force_json:
        # Schema-constrained generation: Groq, OpenAI, OpenRouter, and
        # Ollama's OpenAI endpoint all honor response_format json_object —
        # valid JSON is enforced at GENERATION time, retiring the
        # fence-stripping / truncated-JSON / prose-wrapped-JSON bug family
        # at its source (the parse guards remain as belt-and-braces).
        kwargs["response_format"] = {"type": "json_object"}
    response = _get_openai_compatible_client(provider, provider_cfg).chat.completions.create(
        model=model,
        messages=messages,
        **kwargs,
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


def _dispatch(provider: str, model: str, prompt: str, system: str, max_tokens: int,
              force_json: bool = False) -> _RawResult:
    provider_cfg = _provider_cfg(provider)
    kind = provider_cfg["kind"]
    if kind == "anthropic":
        return _call_anthropic(provider_cfg, model, prompt, system, max_tokens)
    if kind == "gemini":
        return _call_gemini(provider_cfg, model, prompt, system, max_tokens)
    if kind == "openai_compatible":
        return _call_openai_compatible(provider, provider_cfg, model, prompt, system, max_tokens, force_json)
    raise ValueError(f"Unknown provider kind {kind!r} for provider {provider!r}")


_RETRY_BACKOFF_SECONDS = (0.5, 1.5)  # transient errors (rate limits, 503s) are common; retry before giving up

# The most recent successful call's full RoutedResponse — a single-user,
# single-threaded CLI/TUI convenience for displaying "what just answered
# that, how long did it take, how many tokens" without threading metadata
# through every intermediate function's return type. Not meant as a public
# API for concurrent use.
last_call: RoutedResponse | None = None

# Cumulative cost since process start — not persisted, not a substitute for
# a real daily budget tracked across restarts, just enough for a dev
# session to see "am I anywhere near cost_monitor.daily_budget_usd".
session_cost_usd: float = 0.0


def daily_budget_usd() -> float:
    return config.load()["cost_monitor"]["daily_budget_usd"]


def budget_alert_threshold_pct() -> float:
    return config.load()["cost_monitor"]["alert_threshold_pct"]


def call(
    prompt: str,
    system: str = "",
    tier: str = "cheap",
    max_tokens: int = 1024,
    force_json: bool = False,
) -> RoutedResponse:
    global last_call, session_cost_usd
    # Hard stop at the daily budget (plan: "hard budget caps + alerts").
    # Checked against the durable ledger, not session_cost_usd, so a
    # restart can't reset the cap. With all-free tiers spend stays 0 and
    # this never triggers.
    budget = daily_budget_usd()
    spent_today = today_cost_usd()
    if budget > 0 and spent_today >= budget:
        log_event("budget_exhausted", spent_today=spent_today, budget=budget)
        raise ModelProviderError(
            f"daily model budget exhausted (${spent_today:.2f} of ${budget:.2f} spent today) — "
            "raise cost_monitor.daily_budget_usd in config/permissions.yaml or wait for tomorrow"
        )

    tier_cfg = config.load()["model_tiers"][tier]
    model = tier_cfg["model"]
    provider = tier_cfg["provider"]

    if time.monotonic() < _cooldown_until.get(provider, 0.0):
        raise ModelProviderError(
            f"{provider} is cooling down after a rate limit — fallbacks engage immediately"
        )

    last_exc: Exception | None = None
    attempts = len(_RETRY_BACKOFF_SECONDS) + 1
    for attempt in range(attempts):
        start = time.monotonic()
        try:
            raw = _dispatch(provider, model, prompt, system, max_tokens, force_json)
            latency_ms = (time.monotonic() - start) * 1000
            cost_usd = _cost_usd(tier_cfg, raw.usage)
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
                cost_usd=cost_usd,
            )
            response = RoutedResponse(
                text=raw.text,
                tier_used=tier,
                provider=provider,
                model=model,
                latency_ms=latency_ms,
                usage=raw.usage,
                reasoning=raw.reasoning,
                cost_usd=cost_usd,
            )
            last_call = response
            session_cost_usd += cost_usd
            _record_cost(cost_usd)
            _record_tokens(provider, raw.usage)
            return response
        except Exception as exc:
            last_exc = exc
            log_event("model_call_error", tier=tier, provider=provider, model=model, attempt=attempt, error=str(exc))
            if attempt < attempts - 1:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])

    if "429" in str(last_exc) or "rate" in str(last_exc).lower():
        _cooldown_until[provider] = time.monotonic() + _COOLDOWN_S
        log_event("provider_cooldown", provider=provider, seconds=_COOLDOWN_S)
    raise ModelProviderError(f"{provider}/{model} failed after {attempts} attempts: {last_exc}") from last_exc
