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
    "calendar.list",
    "qa.answer",
    "unknown",
]

_SYSTEM_PROMPT = f"""You normalize a short user message into a structured intent.
Valid intents: {", ".join(KNOWN_INTENTS)}.
- reminders.create: setting/adding a new reminder — the user wants to be
  NOTIFIED at some future moment. E.g. "remind me to call mom", "set a
  reminder for 5pm". NOT the same as asking to remember a fact: "remember
  that <fact>" / "note that <fact>" is storing information, not scheduling a
  notification — that's qa.answer, even when the fact mentions a time. E.g.
  "remember that my son's school starts at 8am" -> qa.answer.
- reminders.list: asking whether/what reminders exist — even phrased as a yes/no
  question, this is still reminders.list, not qa.answer. E.g. "what reminders do
  I have", "do I have any reminders?", "any reminders?", "do I have a reminder
  set?", "do i have reminder?".
- reminders.cancel: removing an existing reminder. E.g. "cancel my reminder",
  "delete that reminder".
- calendar.list: asking what's on the user's calendar/schedule for some period.
  E.g. "what's on my calendar today", "any meetings tomorrow?", "am I free
  Friday afternoon?", "what does my week look like". Reminders are Kyraan's
  own; the calendar is external — "do I have reminders" is reminders.list,
  "do I have meetings" is calendar.list.
- qa.answer: everything else conversational — questions, greetings, small talk, or
  anything that isn't about reminders. This should be the common case for
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
    # No max_tokens cap below the router's 1024 default — a reasoning-model
    # tier spends hidden tokens before the visible JSON, and a tight cap
    # truncates the output mid-string (seen live in reminder extraction).
    response = router.call(prompt=raw_text, system=_SYSTEM_PROMPT, tier=tier)
    try:
        data = json.loads(router.strip_code_fence(response.text))

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
