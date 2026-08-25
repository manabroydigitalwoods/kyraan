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
    "calendar.create",
    "home.query",
    "home.control",
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
  "do I have meetings" is calendar.list. And "did you set/create the
  calendar event?" is a question about what Kyraan did — qa.answer, never
  calendar.list.
- calendar.create: adding an event to the user's calendar. E.g. "add a
  meeting with suman tomorrow 5pm to my calendar", "set an event in my
  calendar", "put lunch with mom on the calendar friday 1pm". Details
  given as a follow-up while adding an event ("call suman at 7pm" after
  being asked for the event name) are also calendar.create — fold the
  context in. A calendar EVENT is not a reminder: only an explicit
  reminder request ("remind me...") is reminders.create.
- home.query: asking about the home's smart devices or climate — state,
  power, runtime, temperature. E.g. "is the AC on?", "did I leave the AC
  running?", "how long has the AC been on?", "how much power is the AC
  using?", "what's the bedroom temperature?", "how humid is it inside?".
- home.control: switching a smart device. E.g. "turn off the AC", "switch
  the AC on", "AC off please". Only for device switching — "remind me to
  turn off the AC" is reminders.create.
- qa.answer: everything else conversational — questions, greetings, small talk, or
  anything that isn't about reminders. This should be the common case for
  ordinary chat, not a rare fallback.
- unknown: only for input so garbled or empty that even "have a conversation"
  doesn't apply.
Handle typos, slang, and shorthand. Respond with ONLY a JSON object:
{{"intent": "<one of the valid intents>", "confidence": <0.0-1.0>, "normalized_text": "<cleaned-up message>"}}"""

_CONTEXT_SECTION = """

The message may be a FOLLOW-UP that only makes sense given the recent
conversation below. Use the conversation to resolve it: pick the intent the
user is actually continuing, and write normalized_text as the FULL,
SELF-CONTAINED command with the context folded in. Examples:
- assistant asked which reminder to cancel; user says "the call mom one"
  -> intent reminders.cancel, normalized_text "cancel the call mom reminder"
- reminder time failed earlier; user says "6pm"
  -> intent reminders.create, normalized_text "remind me to call mom at 6pm"
- user mentioned needing to call the plumber; then says "remind me about
  that at 9am" -> reminders.create, "remind me to call the plumber at 9am"
- assistant offered an action; user says "go ahead" / "yes do it"
  -> the offered action's intent, normalized_text spelling that action out.
A message that stands alone is classified as-is — never force context onto it.

Recent conversation (oldest first):
{history}"""


@dataclass
class NormalizedIntent:
    intent: str
    confidence: float
    normalized_text: str


def normalize(raw_text: str, tier: str = "cheap", history: str = "") -> NormalizedIntent:
    # `history` makes the classifier context-aware: follow-ups like "go
    # ahead", "the call mom one", or "6pm" classify as the intent the user
    # is continuing, with normalized_text rewritten self-contained — found
    # live: without it, every follow-up dead-ended as a fresh message.
    system = _SYSTEM_PROMPT + (_CONTEXT_SECTION.format(history=history) if history else "")
    # No max_tokens cap below the router's 1024 default — a reasoning-model
    # tier spends hidden tokens before the visible JSON, and a tight cap
    # truncates the output mid-string (seen live in reminder extraction).
    response = router.call(prompt=raw_text, system=system, tier=tier)
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
