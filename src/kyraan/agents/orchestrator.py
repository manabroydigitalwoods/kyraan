"""Single orchestrator for Phase 1 — no agent router yet (that's Phase 3).

Flow: normalize intent -> gate + dispatch through the kernel -> return text
for the Response Engine (here, just the Telegram send call) to deliver.
"""
import json

from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.kernel import ConfirmationRequired, KillSwitchEngaged, SkillCall
from kyraan.control_plane.logging_setup import log_event
from kyraan.intent.normalize import normalize
from kyraan.model_router import router
from kyraan.triggers import scheduler

_EXTRACT_WHEN_SYSTEM = """Extract a reminder from the user's message.
The current date/time is {now} (includes a UTC offset). Respond with ONLY JSON:
{{"text": "<what to remind about>", "when_iso": "<ISO 8601 datetime, including the same UTC offset as above>"}}"""

_ANSWER_SYSTEM = """You are Kyraan, a personal assistant. The current date/time
is {now}. Be direct and concise — match reply length to the question. A
greeting gets a short, friendly reply, not a lecture about your own
capabilities or limitations. If asked who or what you are, say so plainly
("I'm Kyraan, a personal assistant") rather than deflecting with a generic
"how can I help". Skip disclaimers, meta-commentary about being an AI, and
unsolicited lists of what you can do. You have no visibility here into any
reminder's actual status or countdown — if the question is about a
reminder's state, say you're not sure and suggest checking, never invent
specifics like time remaining. Kyraan genuinely can set reminders,
including short-delay ones — never claim that capability doesn't exist; if
a message looks like a reminder request that landed here by mistake, ask
the user to rephrase it as a clear reminder instead of denying you can do it.
You cannot save facts or remember anything across messages yet — each
message reaches you with no memory of earlier ones. If asked to remember,
note, or save something, say plainly that you can't store memories yet —
never reply "noted" or "got it, I'll remember" or otherwise imply the
information was saved, because it wasn't."""

# Confirm-first flow state: chat_id -> (SkillCall, handler) awaiting a
# yes/no. In-memory only — a restart drops any pending confirmation, which
# fails safe (the action just doesn't run). Phase 1 has no `confirm` skill
# wired into a live intent yet, but the kernel raises ConfirmationRequired
# for any skill the config marks `confirm` (including unlisted ones, which
# default to it), so the path for the user to say "yes" must exist before
# Phase 2 adds tools that rely on it.
_pending_confirmations: dict = {}
_CONFIRM_WORDS = {"yes", "y", "confirm", "ok", "okay", "do it", "go ahead"}
_DENY_WORDS = {"no", "n", "cancel", "don't", "dont", "stop"}


async def handle_message(chat_id: int, raw_text: str) -> str:
    try:
        pending = _pending_confirmations.pop(chat_id, None)
        if pending:
            call, handler = pending
            word = raw_text.strip().lower().rstrip(".!")
            if word in _CONFIRM_WORDS:
                call.confirmed = True
                # Re-runs the full gate: the kill switch is re-checked at
                # confirmation time, not just at the original request.
                return str(await kernel.run_skill(call, handler))
            if word in _DENY_WORDS:
                log_event("confirmation_denied", skill=call.skill_name)
                return f"Okay — '{call.skill_name}' cancelled, nothing was done."
            # Anything else: the user moved on. Drop the pending action
            # (fail safe, never run it implicitly) and handle the new
            # message normally.
            log_event("confirmation_dropped", skill=call.skill_name)

        # Structured JSON intent classification needs more reliability than
        # the cheap tier's local 3B model consistently gives — verified
        # live (2026-08-25): the cheap tier misclassified a clear reminder
        # request ("set reminder in 5mis 'Call to RUma'" got routed to
        # reminders.list) and missed simple questions like "what time is
        # it?"/"who are you?", while frontier (still free, via Groq) was
        # 14/14 correct across the same test set. Classification is a tiny,
        # fast call even on the bigger model — there's no real cost to
        # always using it here, so this isn't a cheap-first-then-escalate
        # dance anymore, just the reliable tier directly.
        parsed = normalize(raw_text, tier="frontier")

        if parsed.confidence < 0.4:
            return "I'm not confident I understood that — could you rephrase?"

        if parsed.intent == "reminders.create":
            return await _create_reminder(chat_id, parsed.normalized_text)
        if parsed.intent == "reminders.list":
            return await _list_reminders(chat_id)
        if parsed.intent == "reminders.cancel":
            return await _cancel_reminder(chat_id, parsed.normalized_text)
        # qa.answer, and anything the classifier couldn't place ("unknown"
        # with reasonable confidence) both fall through here — Phase 1's
        # taxonomy only has one truly distinct path (reminders), so
        # "everything else" should get a best-effort answer rather than a
        # dead-end "I didn't recognize a command" that reads like a broken
        # command parser for what's usually just an ordinary question. Use
        # the model's typo-corrected text for a real qa.answer, but the
        # raw text for "unknown" — normalized_text isn't trustworthy there
        # since the model itself gave up on classifying it.
        text_for_answer = raw_text if parsed.intent == "unknown" else parsed.normalized_text
        return await _answer(chat_id, text_for_answer)
    except KillSwitchEngaged:
        return "The kill switch is engaged — no autonomous action will run until it's disengaged."
    except router.ModelProviderError as exc:
        return f"The model provider failed, not a misunderstanding on my part: {exc}"
    except Exception as exc:
        # Last-resort safety net: a skill handler can fail in ways we can't
        # enumerate up front (a model producing malformed JSON, a garbled
        # datetime, ...). kernel.run_skill re-raises after logging, so
        # without this catch-all an unexpected failure crashes the whole
        # call uncaught — in the TUI that meant the exception propagated
        # out of the worker thread and broke the app's ability to handle
        # any further input. Log the real error, tell the user something
        # generic and safe.
        log_event("handle_message_error", raw_text=raw_text, error=str(exc), error_type=type(exc).__name__)
        return "Something went wrong handling that — try again, or rephrase."


async def _gated(chat_id: int, call: SkillCall, handler) -> str:
    """Run a skill through the kernel; if it needs confirm-first approval,
    stash it and ask, so the next affirmative message from this chat runs it."""
    try:
        return str(await kernel.run_skill(call, handler))
    except ConfirmationRequired:
        _pending_confirmations[chat_id] = (call, handler)
        return f"'{call.skill_name}' needs your confirmation first — reply \"yes\" to run it or \"no\" to cancel."


async def _create_reminder(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        # cheap, backed by llama3.1:8b as of 2026-08-25 — the earlier 3B
        # model (llama3.2) produced malformed JSON here and once embedded
        # prose inside the when_iso value itself, corrupting the datetime
        # outright, so every call was moved to frontier. llama3.1:8b tested
        # clean and correct across every sample (4/4, matching frontier
        # exactly) — see config/permissions.yaml's model_tiers comment.
        # No max_tokens cap below the router's default: a reasoning-model
        # tier (frontier, or if this ever points at one again) spends
        # hidden tokens before the visible JSON, and a 200-token cap
        # truncated the output mid-string live (2026-08-25).
        extraction = router.call(
            prompt=text,
            system=_EXTRACT_WHEN_SYSTEM.format(now=local_now().isoformat()),
            tier="cheap",
        )
        try:
            data = json.loads(extraction.text)
            reminder = scheduler.create_reminder(chat_id, data["text"], data["when_iso"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            # Kept as a safety net even though frontier is far more
            # reliable — no model is perfect, and this must never crash.
            log_event("reminder_extraction_failed", text=text, raw=extraction.text, error=str(exc))
            return "I couldn't work out a time for that reminder — try rephrasing with a clearer date/time."
        return f"Reminder set: \"{data['text']}\" at {data['when_iso']} (id {reminder.id[:8]})"

    return await _gated(chat_id, SkillCall("reminders.create", {"text": text}), handler)


async def _list_reminders(chat_id: int) -> str:
    async def handler(_args: dict) -> str:
        pending = scheduler.store.list_pending(chat_id)
        if not pending:
            return "No pending reminders."
        return "\n".join(f"- [{r.id[:8]}] {r.text} at {r.when_iso}" for r in pending)

    return await _gated(chat_id, SkillCall("reminders.list", {}), handler)


async def _cancel_reminder(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        pending = scheduler.store.list_pending(chat_id)
        if not pending:
            return "No matching reminder found to cancel."
        # Match any token in the message against pending ids — the old
        # single-token extraction grabbed the *first* >=6-char word, which
        # was usually "cancel" itself, so a real id later in the message
        # never matched. Ordinary words can't collide with a uuid-hex
        # prefix ("cancel" contains non-hex letters), so checking every
        # token is safe.
        tokens = [t.lower() for t in args["text"].split() if len(t) >= 6 and t.isalnum()]
        match = next((r for r in pending if any(r.id.startswith(t) for t in tokens)), None)
        if not match and len(pending) == 1:
            # Only one reminder exists — "cancel my reminder" is unambiguous.
            match = pending[0]
        if not match:
            # Several pending and no id in the message: cancelling a guess
            # is destructive and silent when it's wrong (a live walkthrough
            # only passed here because the intended reminder happened to be
            # first in the list). Ask instead.
            listing = "\n".join(f"- [{r.id[:8]}] {r.text} at {r.when_iso}" for r in pending)
            return (
                "You have more than one pending reminder — which should I cancel? "
                f"Reply like \"cancel {pending[0].id[:8]}\":\n{listing}"
            )
        scheduler.cancel_reminder(match.id)
        return f"Cancelled reminder: \"{match.text}\""

    return await _gated(chat_id, SkillCall("reminders.cancel", {"text": text}), handler)


async def _answer(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        # cheap, backed by llama3.1:8b as of 2026-08-25 — asked "what time
        # is it?" with the correct current time given directly in the
        # system prompt, the earlier 3B model (llama3.2) answered wrong in
        # 3/3 tries (14:40, 17:30, 15:50 — actual was 13:50); llama3.1:8b
        # was exactly right in 3/3, matching frontier. See
        # config/permissions.yaml's model_tiers comment.
        response = router.call(
            prompt=args["text"], system=_ANSWER_SYSTEM.format(now=local_now().isoformat()), tier="cheap"
        )
        return response.text

    return await _gated(chat_id, SkillCall("qa.answer", {"text": text}), handler)
