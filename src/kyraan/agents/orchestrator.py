"""Single orchestrator for Phase 1 — no agent router yet (that's Phase 3).

Flow: normalize intent -> gate + dispatch through the kernel -> return text
for the Response Engine (here, just the Telegram send call) to deliver.
"""
import json

from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.kernel import ConfirmationRequired, KillSwitchEngaged, SkillCall
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
unsolicited lists of what you can do."""


async def handle_message(chat_id: int, raw_text: str) -> str:
    try:
        parsed = normalize(raw_text)
        if parsed.confidence < 0.4:
            # The cheap tier is a small local model — low confidence is
            # sometimes genuine ambiguity, but often just that model's
            # sampling variance on an easy input. Retry once on the
            # frontier tier before giving up and asking the user to
            # rephrase something a bigger model would have understood fine.
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
        return await _answer(text_for_answer)
    except ConfirmationRequired as exc:
        return f"'{exc.skill_name}' needs your confirmation first — this shouldn't happen for auto-permission skills, check config/permissions.yaml."
    except KillSwitchEngaged:
        return "The kill switch is engaged — no autonomous action will run until it's disengaged."
    except router.ModelProviderError as exc:
        return f"The model provider failed, not a misunderstanding on my part: {exc}"


async def _create_reminder(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        extraction = router.call(
            prompt=text,
            system=_EXTRACT_WHEN_SYSTEM.format(now=local_now().isoformat()),
            tier="cheap",
            max_tokens=200,
        )
        data = json.loads(extraction.text)
        reminder = scheduler.create_reminder(chat_id, data["text"], data["when_iso"])
        return f"Reminder set: \"{data['text']}\" at {data['when_iso']} (id {reminder.id[:8]})"

    return await kernel.run_skill(SkillCall("reminders.create", {"text": text}), handler)


async def _list_reminders(chat_id: int) -> str:
    async def handler(_args: dict) -> str:
        pending = scheduler.store.list_pending(chat_id)
        if not pending:
            return "No pending reminders."
        return "\n".join(f"- [{r.id[:8]}] {r.text} at {r.when_iso}" for r in pending)

    return await kernel.run_skill(SkillCall("reminders.list", {}), handler)


async def _cancel_reminder(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        pending = scheduler.store.list_pending(chat_id)
        match = next((r for r in pending if r.id.startswith(args["ref"])), None) if args.get("ref") else None
        if not match and pending:
            match = pending[0]
        if not match:
            return "No matching reminder found to cancel."
        scheduler.cancel_reminder(match.id)
        return f"Cancelled reminder: \"{match.text}\""

    ref = next((tok for tok in text.split() if len(tok) >= 6 and tok.isalnum()), "")
    return await kernel.run_skill(SkillCall("reminders.cancel", {"ref": ref}), handler)


async def _answer(text: str) -> str:
    async def handler(args: dict) -> str:
        response = router.call_with_escalation(
            prompt=args["text"], system=_ANSWER_SYSTEM.format(now=local_now().isoformat())
        )
        return response.text

    return await kernel.run_skill(SkillCall("qa.answer", {"text": text}), handler)
