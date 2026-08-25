"""Two-tier model routing: cheap by default, escalate to frontier on request
or when the cheap tier reports low confidence.
"""
import os
from dataclasses import dataclass

from anthropic import Anthropic

from kyraan.control_plane import config
from kyraan.control_plane.logging_setup import log_event

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


@dataclass
class RoutedResponse:
    text: str
    tier_used: str
    model: str


def call(
    prompt: str,
    system: str = "",
    tier: str = "cheap",
    max_tokens: int = 1024,
) -> RoutedResponse:
    model = config.model_for_tier(tier)
    client = _get_client()

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system or None,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")

    log_event("model_call", tier=tier, model=model, prompt_chars=len(prompt))
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
