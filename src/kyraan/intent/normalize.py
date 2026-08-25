"""Intent normalization: resolve typos/slang/shorthand into a structured
intent + confidence using the cheap model tier. Escalation to a bigger
model or a clarifying question is the caller's decision, not this module's.
"""
import json
from dataclasses import dataclass

from kyraan.model_router import router

KNOWN_INTENTS = [
    "reminders.create",
    "reminders.list",
    "reminders.cancel",
    "qa.answer",
    "unknown",
]

_SYSTEM_PROMPT = f"""You normalize a short user message into a structured intent.
Valid intents: {", ".join(KNOWN_INTENTS)}.
- reminders.create/list/cancel: anything about setting, listing, or cancelling a reminder.
- qa.answer: everything else conversational — questions, greetings, small talk, or
  anything that isn't a reminder request. This should be the common case for
  ordinary chat, not a rare fallback.
- unknown: only for input so garbled or empty that even "have a conversation"
  doesn't apply.
Handle typos, slang, and shorthand. Respond with ONLY a JSON object:
{{"intent": "<one of the valid intents>", "confidence": <0.0-1.0>, "normalized_text": "<cleaned-up message>"}}"""


@dataclass
class NormalizedIntent:
    intent: str
    confidence: float
    normalized_text: str


def normalize(raw_text: str, tier: str = "cheap") -> NormalizedIntent:
    response = router.call(prompt=raw_text, system=_SYSTEM_PROMPT, tier=tier, max_tokens=200)
    try:
        data = json.loads(response.text)

        # A small local model won't always follow the schema exactly — a
        # field can come back JSON null (dict.get's default only applies to
        # a *missing* key, not one present with value null) or `intent` can
        # be a string outside KNOWN_INTENTS. Treat either as "unknown"
        # rather than crashing or silently passing through the caller's
        # exact-match dispatch.
        intent = data.get("intent") or "unknown"
        if intent not in KNOWN_INTENTS:
            intent = "unknown"

        confidence = data.get("confidence")
        confidence = float(confidence) if confidence is not None else 0.0

        normalized_text = data.get("normalized_text") or raw_text

        return NormalizedIntent(intent=intent, confidence=confidence, normalized_text=normalized_text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return NormalizedIntent(intent="unknown", confidence=0.0, normalized_text=raw_text)
