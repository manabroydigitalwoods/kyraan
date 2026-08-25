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
Handle typos, slang, and shorthand. Respond with ONLY a JSON object:
{{"intent": "<one of the valid intents>", "confidence": <0.0-1.0>, "normalized_text": "<cleaned-up message>"}}
If nothing fits, use intent "unknown" with a low confidence."""


@dataclass
class NormalizedIntent:
    intent: str
    confidence: float
    normalized_text: str


def normalize(raw_text: str) -> NormalizedIntent:
    response = router.call(prompt=raw_text, system=_SYSTEM_PROMPT, tier="cheap", max_tokens=200)
    try:
        data = json.loads(response.text)
        return NormalizedIntent(
            intent=data.get("intent", "unknown"),
            confidence=float(data.get("confidence", 0.0)),
            normalized_text=data.get("normalized_text", raw_text),
        )
    except (json.JSONDecodeError, ValueError):
        return NormalizedIntent(intent="unknown", confidence=0.0, normalized_text=raw_text)
