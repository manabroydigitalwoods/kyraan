"""Single orchestrator for Phase 1 — no agent router yet (that's Phase 3).

Flow: normalize intent -> gate + dispatch through the kernel -> return text
for the Response Engine (here, just the Telegram send call) to deliver.
"""
import json
import os
from datetime import datetime

from kyraan.control_plane import kernel
from kyraan.control_plane.kernel import ConfirmationRequired, KillSwitchEngaged, SkillCall
from kyraan.intent.normalize import normalize
from kyraan.model_router import router
from kyraan.triggers import scheduler

_EXTRACT_WHEN_SYSTEM = """Extract a reminder from the user's message.
The current date/time is {now}. Respond with ONLY JSON:
{{"text": "<what to remind about>", "when_iso": "<ISO 8601 datetime>"}}"""


async def handle_message(chat_id: int, raw_text: str) -> str:
    parsed = normalize(raw_text)

    if parsed.confidence < 0.4:
        return "I'm not confident I understood that — could you rephrase?"

    try:
        if parsed.intent == "reminders.create":
            return await _create_reminder(chat_id, parsed.normalized_text)
        if parsed.intent == "reminders.list":
            return await _list_reminders(chat_id)
        if parsed.intent == "reminders.cancel":
            return await _cancel_reminder(chat_id, parsed.normalized_text)
        if parsed.intent == "qa.answer":
            return await _answer(parsed.normalized_text)
        return "I didn't recognize a supported command yet — Phase 1 only handles reminders and Q&A."
    except ConfirmationRequired as exc:
        return f"'{exc.skill_name}' needs your confirmation first — this shouldn't happen for auto-permission skills, check config/permissions.yaml."
    except KillSwitchEngaged:
        return "The kill switch is engaged — no autonomous action will run until it's disengaged."


async def _create_reminder(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        extraction = router.call(
            prompt=text,
            system=_EXTRACT_WHEN_SYSTEM.format(now=datetime.now().isoformat()),
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
        response = router.call_with_escalation(prompt=args["text"])
        return response.text

    return await kernel.run_skill(SkillCall("qa.answer", {"text": text}), handler)
