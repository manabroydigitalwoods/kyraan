"""Single orchestrator for Phase 1 — no agent router yet (that's Phase 3).

Flow: normalize intent -> gate + dispatch through the kernel -> return text
for the Response Engine (here, just the Telegram send call) to deliver.
"""
import json
from collections import defaultdict, deque

from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.kernel import ConfirmationRequired, KillSwitchEngaged, SkillCall
from kyraan.control_plane.logging_setup import log_event
from kyraan.intent.normalize import normalize
from kyraan.memory import extraction
from kyraan.memory import store as memory_store
from kyraan.model_router import router
from kyraan.triggers import scheduler

_EXTRACT_WINDOW_SYSTEM = """The user is asking what's on their calendar.
The current date/time is {now} (includes a UTC offset). Work out the time
window they mean — default to the rest of today if unclear; "tomorrow" is
that full day; "this week" runs to Sunday night. Respond with ONLY JSON:
{{"start_iso": "<ISO 8601 datetime with the same UTC offset>", "end_iso": "<ISO 8601 datetime with the same UTC offset>", "label": "<short human name for the window, e.g. 'today', 'tomorrow'>"}}"""

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
Kyraan can also read the owner's Google Calendar — never deny that either;
if a calendar question lands here by mistake, suggest phrasing it like
"what's on my calendar today".

Known facts, from human-reviewed memory — treat these as true, and never
invent personal facts that aren't listed here or in the conversation below.
If asked for a PERSONAL fact found in neither, say you don't know it yet.
This applies only to facts about the user's life — general-knowledge
questions (geography, code, science, anything public) have nothing to do
with this memory; answer them normally from your own knowledge:
{facts}

Recent conversation, oldest first — use it to resolve follow-ups and
pronouns; it is your only memory of this session:
{history}

When the user states a new fact, respond naturally — a separate extraction
step queues stated facts for human review automatically, and the reply is
annotated when that happens. Never claim a fact is already permanently
saved (facts go live only after review), and never deny being able to
remember — if asked, say new facts are saved after a quick review step."""

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

# Rolling per-chat conversation window: the qa.answer prompt's only session
# memory. In-memory on purpose (like _pending_confirmations) — a restart
# forgets the conversation, which is honest, and durable facts are the
# memory tree's job, not this window's.
_HISTORY_MAX_ENTRIES = 20  # 10 user/assistant exchanges
_history: dict = defaultdict(lambda: deque(maxlen=_HISTORY_MAX_ENTRIES))

# Below this length a message can't state a durable fact ("yes", "hi",
# "thanks") — skip the extraction model call entirely.
_EXTRACTION_MIN_CHARS = 8


def _history_block(chat_id: int) -> str:
    return "\n".join(f"{role}: {text}" for role, text in _history[chat_id]) or "(no conversation yet)"


async def _extraction_note(raw_text: str) -> str:
    """Run fact extraction and return a reply suffix naming what was queued
    ("" when nothing was). Extraction is best-effort: it must never break
    or replace the actual reply, so every failure is logged and swallowed."""
    if len(raw_text.strip()) < _EXTRACTION_MIN_CHARS:
        return ""
    try:
        queued = await extraction.propose_from_message(raw_text)
    except Exception as exc:
        log_event("extraction_error", error=str(exc), error_type=type(exc).__name__)
        return ""
    if not queued:
        return ""
    facts = "; ".join(f.lstrip("- ").strip() for f in queued)
    return f"\n\n📝 Noted for review: {facts}"


async def handle_message(chat_id: int, raw_text: str) -> str:
    reply = await _dispatch(chat_id, raw_text)
    reply += await _extraction_note(raw_text)
    if router.budget_alert_due():
        reply += (
            f"\n\n⚠️ Model spend today is ${router.today_cost_usd():.2f} — past "
            f"{router.budget_alert_threshold_pct():.0f}% of the ${router.daily_budget_usd():.2f} "
            "daily budget. Calls stop at the cap."
        )
    _history[chat_id].append(("user", raw_text))
    _history[chat_id].append(("assistant", reply))
    return reply


async def _dispatch(chat_id: int, raw_text: str) -> str:
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
        try:
            parsed = normalize(raw_text, tier="frontier")
        except router.ModelProviderError as exc:
            # Frontier (Groq) is classification's single cloud dependency —
            # if it's down or rate-limited, degrade to the local cheap tier
            # (measured at 12-13/14 on the same test set) instead of
            # refusing to understand anything at all.
            log_event("intent_fallback_cheap", error=str(exc))
            parsed = normalize(raw_text, tier="cheap")

        if parsed.confidence < 0.4:
            return "I'm not confident I understood that — could you rephrase?"

        if parsed.intent == "reminders.create":
            return await _create_reminder(chat_id, parsed.normalized_text)
        if parsed.intent == "reminders.list":
            return await _list_reminders(chat_id)
        if parsed.intent == "reminders.cancel":
            return await _cancel_reminder(chat_id, parsed.normalized_text)
        if parsed.intent == "calendar.list":
            return await _list_calendar(chat_id, parsed.normalized_text)
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
        extracted = router.call(
            prompt=text,
            system=_EXTRACT_WHEN_SYSTEM.format(now=local_now().isoformat()),
            tier="cheap",
        )
        try:
            data = json.loads(router.strip_code_fence(extracted.text))
            reminder = scheduler.create_reminder(chat_id, data["text"], data["when_iso"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            # Kept as a safety net even though frontier is far more
            # reliable — no model is perfect, and this must never crash.
            log_event("reminder_extraction_failed", text=text, raw=extracted.text, error=str(exc))
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


async def _list_calendar(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        window = router.call(
            prompt=args["text"],
            system=_EXTRACT_WINDOW_SYSTEM.format(now=local_now().isoformat()),
            tier="cheap",
        )
        try:
            data = json.loads(router.strip_code_fence(window.text))
            start, end = data["start_iso"], data["end_iso"]
            label = data.get("label") or "that period"
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log_event("calendar_window_extraction_failed", text=args["text"], raw=window.text, error=str(exc))
            return "I couldn't work out which period you mean — try e.g. \"what's on my calendar tomorrow?\""

        try:
            events = await kernel.run_tool(kernel.ToolCall("calendar.list_events", {"start": start, "end": end}))
        except kernel.ToolFailed as exc:
            # on_failure: surface — the message is written to be shown.
            return f"Couldn't check the calendar: {exc}"

        if not events:
            return f"Nothing on the calendar {label}."
        lines = []
        for e in events:
            when = "all day" if e["all_day"] else e["start"][11:16]
            where = f" ({e['location']})" if e.get("location") else ""
            lines.append(f"- {when} — {e['title']}{where}")
        return f"Calendar {label}:\n" + "\n".join(lines)

    return await _gated(chat_id, SkillCall("calendar.list", {"text": text}), handler)


async def _answer(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        # cheap, backed by llama3.1:8b as of 2026-08-25 — asked "what time
        # is it?" with the correct current time given directly in the
        # system prompt, the earlier 3B model (llama3.2) answered wrong in
        # 3/3 tries (14:40, 17:30, 15:50 — actual was 13:50); llama3.1:8b
        # was exactly right in 3/3, matching frontier. See
        # config/permissions.yaml's model_tiers comment.
        response = router.call(
            prompt=args["text"],
            system=_ANSWER_SYSTEM.format(
                now=local_now().isoformat(),
                facts=memory_store.load_all_facts() or "(no facts stored yet)",
                history=_history_block(chat_id),
            ),
            tier="cheap",
        )
        return response.text

    return await _gated(chat_id, SkillCall("qa.answer", {"text": text}), handler)
