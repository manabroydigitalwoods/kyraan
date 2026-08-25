"""Single orchestrator for Phase 1 — no agent router yet (that's Phase 3).

Flow: normalize intent -> gate + dispatch through the kernel -> return text
for the Response Engine (here, just the Telegram send call) to deliver.
"""
import contextvars
import json
import time
from collections import defaultdict, deque

from kyraan.agents.capabilities import capability_brief
from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import humanize, local_now
from kyraan.control_plane.kernel import ConfirmationRequired, KillSwitchEngaged, SkillCall
from kyraan.control_plane.logging_setup import log_chat, log_event
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

_EXTRACT_EVENT_SYSTEM = """Extract a calendar event from the user's message.
The current date/time is {now} (includes a UTC offset). Use a stated clock
time EXACTLY — "5pm" means 17:00:00, never the current minutes/seconds or
microseconds carried over. If no end time is given, make the event 1 hour
long. location is JSON null when no place was mentioned — never the string
"null". Respond with ONLY JSON:
{{"title": "<short event title>", "start_iso": "<ISO 8601, same UTC offset as above>", "end_iso": "<ISO 8601, same offset>", "location": "<place or null>"}}"""

_EXTRACT_WHEN_SYSTEM = """Extract a reminder from the user's message.
The current date/time is {now} (includes a UTC offset). Use a stated clock
time EXACTLY — "8pm" means 20:00:00, never the current minutes/seconds
carried over from now. Respond with ONLY JSON:
{{"text": "<what to remind about>", "when_iso": "<ISO 8601 datetime, including the same UTC offset as above>"}}"""

_ANSWER_SYSTEM = """You are Kyraan, a personal assistant. The current date/time
is {now}. Respond the way a capable, trusted human assistant would: direct,
natural, matched in length to the question. A greeting gets a short friendly
reply. If asked who you are: "I'm Kyraan, a personal assistant." Skip
disclaimers, meta-commentary about being an AI, and unsolicited lists of
what you can do.

{capabilities}

HONESTY RULES, absolute:
- Never claim an action happened (event created, device switched, reminder
  set, fact saved) unless it actually did. A reminder is not a calendar
  event — never present one as the other.
- You have no visibility into a reminder's live status or countdown — if
  asked, say you're not sure and suggest checking; never invent specifics.
- Facts the user tells you are saved only after the owner's review — say
  "it'll be saved after a quick review", never that it's already permanently
  saved. Never deny being able to remember.
- If a request maps to a live capability but landed here by mistake,
  suggest the phrasing that works ("what's on my calendar today", "is the
  AC on?", "any new emails?") instead of denying the capability.

When the user asks you to CREATE something — a song, poem, story, message,
code — ask at most ONE clarifying question, then create it. "anything",
"random", "you choose", "go ahead", "yes" mean: stop asking and produce it
NOW, in full, using the conversation to know what "it" is. A request to
change length, format, or style ("make it 2 paragraphs", "shorter",
"more formal") applies to YOUR PREVIOUS creation — produce the revised
version; repeating the previous text unchanged is never an answer. Never
answer about schedules or tasks while a creative thread is live.

Known facts, from the owner-reviewed memory — treat as true; never invent
personal facts beyond these and the conversation. When the user states a
fact you ALREADY have in this list, say you already know it — don't
promise to save it again. If asked for a PERSONAL
fact in neither, say you don't know it yet (general knowledge — geography,
code, science — is unaffected; answer normally):
{facts}

Facts the user has STATED but the owner hasn't reviewed yet — use them to
answer (the user said them), and mention they're still awaiting the
owner's review when relevant:
{pending_facts}

Recent conversation, oldest first — your only memory of this session; use
it to resolve follow-ups and pronouns:
{history}"""

# Confirm-first flow state: chat_id -> (SkillCall, handler) awaiting a
# yes/no. In-memory only — a restart drops any pending confirmation, which
# fails safe (the action just doesn't run). Phase 1 has no `confirm` skill
# wired into a live intent yet, but the kernel raises ConfirmationRequired
# for any skill the config marks `confirm` (including unlisted ones, which
# default to it), so the path for the user to say "yes" must exist before
# Phase 2 adds tools that rely on it.
_pending_confirmations: dict = {}
# A pending confirmation goes stale: "About to turn the AC ON" asked at
# noon must not execute on an unrelated "yes" hours later. Physical
# actions deserve freshness.
_CONFIRMATION_TTL_S = 300
_CONFIRM_WORDS = {"yes", "y", "confirm", "ok", "okay", "do it", "go ahead"}
_DENY_WORDS = {"no", "n", "cancel", "don't", "dont", "stop"}

# Rolling per-chat conversation window: the qa.answer prompt's only session
# memory. In-memory on purpose (like _pending_confirmations) — a restart
# forgets the conversation, which is honest, and durable facts are the
# memory tree's job, not this window's.
_HISTORY_MAX_ENTRIES = 40  # 20 exchanges — 20 rolled out mid-session live
                           # ("you never shared Mamata data" after 17 turns)
_history: dict = defaultdict(lambda: deque(maxlen=_HISTORY_MAX_ENTRIES))

# Below this length a message can't state a durable fact ("yes", "hi",
# "thanks") — skip the extraction model call entirely.
_EXTRACTION_MIN_CHARS = 8

# Data boundary: a skill can replace what the conversation history records
# for its reply. Email summaries use this — sender/subject lines must not
# ride the history into the cloud classifier or qa prompts (owner's §3a
# resolution: work email metadata stays off third-party models).
_history_redaction: contextvars.ContextVar = contextvars.ContextVar("history_redaction", default=None)
_skip_extraction: contextvars.ContextVar = contextvars.ContextVar("skip_extraction", default=False)


_CLOCK_RE = None


def _anchor_clock_time(raw_text: str, when_iso: str) -> str:
    """The user's explicitly stated clock time ALWAYS beats the model's
    extraction — seen live twice: "8pm" extracted as 20:49, and "9pm"
    extracted as 8:00 PM. When the message contains exactly one am/pm
    clock time, the extracted datetime's clock is corrected to it
    deterministically (date and timezone kept from the model)."""
    global _CLOCK_RE
    import re
    if _CLOCK_RE is None:
        _CLOCK_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
    matches = _CLOCK_RE.findall(raw_text)
    if len(matches) != 1:
        return when_iso
    hh, mm, ap = matches[0]
    hour = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    minute = int(mm) if mm else 0
    parsed = scheduler._parse_when(when_iso)
    if (parsed.hour, parsed.minute) == (hour, minute):
        return when_iso
    corrected = parsed.replace(hour=hour, minute=minute, second=0, microsecond=0)
    log_event("clock_time_anchored", stated=f"{hh}:{mm or '00'}{ap}", model_gave=when_iso,
              corrected=corrected.isoformat())
    return corrected.isoformat()


def record_proactive(chat_id: int, text: str) -> None:
    """Proactive sends (reminders, briefs) belong in conversation history
    too — found live: \"Thanks for the reminder\" got \"I didn't actually
    send you any reminders\" because fire() bypassed _history entirely."""
    _history[chat_id].append(("assistant", text))
    log_chat(chat_id, "proactive", text)


def _history_block(chat_id: int, clip: int = 600) -> str:
    """Per-entry clip: one pasted article must not drown the prompt (and
    with Ollama's default 4K context it literally truncated the system
    instructions — the likely cause of a live garbled reply)."""
    rendered = "\n".join(
        f"{role}: {text[:clip] + '…' if len(text) > clip else text}"
        for role, text in _history[chat_id]
    )
    return rendered or "(no conversation yet)"


def _classifier_context(chat_id: int, entries: int = 6, clip: int = 200) -> str:
    """Compact tail of the conversation for intent classification — enough
    to resolve a follow-up, clipped so a long calendar listing or code
    answer doesn't drown the classifier prompt."""
    recent = list(_history[chat_id])[-entries:]
    return "\n".join(
        f"{role}: {text[:clip] + '…' if len(text) > clip else text}" for role, text in recent
    )


def _structured_call(prompt: str, system: str):
    """Structured extraction (reminder times, event fields, calendar
    windows) is exactness-critical: frontier first, local fallback when
    the cloud tier is down/exhausted — walkthrough v3 (degraded mode)
    showed the local 8B extracting \"in 45mins\" as a PAST time and
    failing window JSON outright."""
    try:
        return router.call(prompt=prompt, system=system, tier="frontier")
    except router.ModelProviderError as exc:
        log_event("structured_fallback_cheap", error=str(exc))
        return router.call(prompt=prompt, system=system, tier="cheap")


async def _extraction_note(chat_id: int, raw_text: str) -> str:
    """Run fact extraction and return a reply suffix naming what was queued
    ("" when nothing was). Extraction is best-effort: it must never break
    or replace the actual reply, so every failure is logged and swallowed."""
    if len(raw_text.strip()) < _EXTRACTION_MIN_CHARS:
        return ""
    try:
        queued = await extraction.propose_from_message(raw_text, context=_classifier_context(chat_id))
    except Exception as exc:
        log_event("extraction_error", error=str(exc), error_type=type(exc).__name__)
        return ""
    if not queued:
        return ""
    facts = "; ".join(f.lstrip("- ").strip() for f in queued)
    return f"\n\n📝 Noted for review: {facts}"


async def handle_message(chat_id: int, raw_text: str) -> str:
    redaction_token = _history_redaction.set(None)
    skip_token = _skip_extraction.set(False)
    reply = await _dispatch(chat_id, raw_text)
    if not _skip_extraction.get():
        reply += await _extraction_note(chat_id, raw_text)
    _skip_extraction.reset(skip_token)
    quota_warning = router.quota_alert_due()
    if quota_warning:
        reply += f"\n\n⚠️ {quota_warning}."
    if router.budget_alert_due():
        reply += (
            f"\n\n⚠️ Model spend today is ${router.today_cost_usd():.2f} — past "
            f"{router.budget_alert_threshold_pct():.0f}% of the ${router.daily_budget_usd():.2f} "
            "daily budget. Calls stop at the cap."
        )
    _history[chat_id].append(("user", raw_text))
    _history[chat_id].append(("assistant", _history_redaction.get() or reply))
    _history_redaction.reset(redaction_token)
    log_chat(chat_id, "user", raw_text)
    log_chat(chat_id, "assistant", reply)
    return reply


async def _dispatch(chat_id: int, raw_text: str) -> str:
    try:
        pending = _pending_confirmations.pop(chat_id, None)
        if pending:
            call, handler, stashed_at = pending
            word = raw_text.strip().lower().rstrip(".!")
            if time.monotonic() - stashed_at > _CONFIRMATION_TTL_S:
                log_event("confirmation_expired", skill=call.skill_name)
                if word in _CONFIRM_WORDS or word in _DENY_WORDS:
                    return (
                        f"That confirmation for '{call.skill_name}' expired "
                        "(over 5 minutes old) — ask again if you still want it."
                    )
                # An unrelated message: drop the stale ask silently and
                # handle the new message normally.
            elif word in _CONFIRM_WORDS:
                call.confirmed = True
                # Re-runs the full gate: the kill switch is re-checked at
                # confirmation time, not just at the original request.
                return str(await kernel.run_skill(call, handler))
            elif word in _DENY_WORDS:
                log_event("confirmation_denied", skill=call.skill_name)
                return f"Okay — '{call.skill_name}' cancelled, nothing was done."
            else:
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
        context = _classifier_context(chat_id)
        try:
            parsed = normalize(raw_text, tier="frontier", history=context)
        except router.ModelProviderError as exc:
            # Frontier (Groq) is classification's single cloud dependency —
            # if it's down or rate-limited, degrade to the local cheap tier
            # (measured at 12-13/14 on the same test set) instead of
            # refusing to understand anything at all.
            log_event("intent_fallback_cheap", error=str(exc))
            parsed = normalize(raw_text, tier="cheap", history=context)

        # Sanity-guard the rewrite: a degraded classifier was seen turning
        # "Do you know my father?" into the ANSWER "I don't have any
        # information about your family members." — which qa then answered
        # instead of the user's words. A legit contextual rewrite draws its
        # words from the message or the recent conversation; one that draws
        # from neither is hallucination — use the raw text.
        def _words(t):
            return {w.strip(".,!?'\"").lower() for w in t.split() if len(w) > 2}
        import difflib
        norm_w = _words(parsed.normalized_text)
        allowed = _words(raw_text) | _words(context)
        word_grounded = bool(norm_w) and len(norm_w & allowed) / len(norm_w) >= 0.3
        # Typo corrections change the words themselves ("wat tym" ->
        # "what time"), so textual similarity is the other legitimate
        # path — the guard was seen rejecting a correct typo fix.
        textually_close = difflib.SequenceMatcher(
            None, raw_text.lower(), parsed.normalized_text.lower()
        ).ratio() >= 0.5
        if norm_w and not word_grounded and not textually_close:
            log_event("normalized_text_rejected", chat_id=chat_id,
                      normalized=parsed.normalized_text, raw=raw_text)
            parsed.normalized_text = raw_text
        log_event("intent_classified", chat_id=chat_id, intent=parsed.intent,
                  confidence=parsed.confidence, normalized=parsed.normalized_text)
        if parsed.confidence < 0.4:
            _skip_extraction.set(True)  # a message we couldn't parse can't state reliable facts
            return "I'm not confident I understood that — could you rephrase?"

        if parsed.intent == "reminders.create":
            return await _create_reminder(chat_id, parsed.normalized_text)
        if parsed.intent == "reminders.list":
            return await _list_reminders(chat_id)
        if parsed.intent == "reminders.cancel":
            return await _cancel_reminder(chat_id, parsed.normalized_text)
        if parsed.intent == "calendar.list":
            return await _list_calendar(chat_id, parsed.normalized_text)
        if parsed.intent == "calendar.create":
            return await _create_event(chat_id, parsed.normalized_text)
        if parsed.intent == "email.check":
            return await _check_email(chat_id, parsed.normalized_text)
        if parsed.intent == "home.query":
            return await _home_query(chat_id, parsed.normalized_text)
        if parsed.intent == "home.control":
            return await _home_control(chat_id, parsed.normalized_text)
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
        # Full detail (org ids, billing links) belongs in the log, not the
        # chat — seen live: a Groq 429 dumped its entire raw error into
        # Telegram.
        log_event("model_provider_error", error=str(exc))
        hint = " (rate limit — it resets on a rolling window)" if "429" in str(exc) or "rate" in str(exc).lower() else ""
        return f"The AI provider is having trouble right now{hint} — try again in a few minutes."
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


async def _gated(chat_id: int, call: SkillCall, handler, describe: str = "") -> str:
    """Run a skill through the kernel; if it (or a confirm-gated tool
    inside it) needs approval, stash it and ask — `describe` names the
    concrete action so the user confirms a specific thing, not a vague
    intent."""
    try:
        return str(await kernel.run_skill(call, handler))
    except ConfirmationRequired:
        _pending_confirmations[chat_id] = (call, handler, time.monotonic())
        what = describe or f"'{call.skill_name}' needs your confirmation first"
        return f"{what} — reply \"yes\" to confirm or \"no\" to cancel."


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
        extracted = _structured_call(text, _EXTRACT_WHEN_SYSTEM.format(now=local_now().isoformat()))
        try:
            data = json.loads(router.strip_code_fence(extracted.text))
            data["when_iso"] = _anchor_clock_time(text, data["when_iso"])
            existing = scheduler.find_duplicate(chat_id, data["text"], data["when_iso"])
            if existing:
                return (
                    f"Already set: \"{existing.text}\" at {humanize(existing.when_iso)} "
                    f"(id {existing.id[:8]}) — I didn't add a duplicate."
                )
            reminder = scheduler.create_reminder(chat_id, data["text"], data["when_iso"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            # Kept as a safety net even though frontier is far more
            # reliable — no model is perfect, and this must never crash.
            log_event("reminder_extraction_failed", text=text, raw=extracted.text, error=str(exc))
            return "I couldn't work out a time for that reminder — try rephrasing with a clearer date/time."
        return f"Reminder set: \"{data['text']}\" at {humanize(data['when_iso'])} (id {reminder.id[:8]})"

    return await _gated(chat_id, SkillCall("reminders.create", {"text": text}), handler)


async def _list_reminders(chat_id: int) -> str:
    async def handler(_args: dict) -> str:
        pending = scheduler.store.list_pending(chat_id)
        if not pending:
            return "No pending reminders."
        return "\n".join(f"- [{r.id[:8]}] {r.text} at {humanize(r.when_iso)}" for r in pending)

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
        if not match:
            # Match by description too ("cancel the call mom one") — the
            # context-aware classifier rewrites follow-ups into phrases
            # like this, and demanding an id for them dead-ends the
            # conversation. Only an UNAMBIGUOUS description match cancels;
            # words hitting several reminders still ask.
            stop = {"cancel", "the", "reminder", "reminders", "one", "that",
                    "this", "delete", "remove", "please", "for", "about", "set"}
            words = [w.lower().strip(".,!?'\"") for w in args["text"].split()]
            words = [w for w in words if len(w) >= 3 and w not in stop]
            candidates = [r for r in pending if any(w in r.text.lower() for w in words)] if words else []
            if len(candidates) == 1:
                match = candidates[0]
        if not match and len(pending) == 1:
            # Only one reminder exists — "cancel my reminder" is unambiguous.
            match = pending[0]
        if not match:
            # Several pending and no id in the message: cancelling a guess
            # is destructive and silent when it's wrong (a live walkthrough
            # only passed here because the intended reminder happened to be
            # first in the list). Ask instead.
            listing = "\n".join(f"- [{r.id[:8]}] {r.text} at {humanize(r.when_iso)}" for r in pending)
            return (
                "You have more than one pending reminder — which should I cancel? "
                f"Reply like \"cancel {pending[0].id[:8]}\":\n{listing}"
            )
        scheduler.cancel_reminder(match.id)
        return f"Cancelled reminder: \"{match.text}\""

    return await _gated(chat_id, SkillCall("reminders.cancel", {"text": text}), handler)


async def _list_calendar(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        window = _structured_call(args["text"], _EXTRACT_WINDOW_SYSTEM.format(now=local_now().isoformat()))
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
            when = "all day" if e["all_day"] else humanize(e["start"])
            where = f" ({e['location']})" if e.get("location") else ""
            lines.append(f"- {when} — {e['title']}{where}")
        return f"Calendar {label}:\n" + "\n".join(lines)

    return await _gated(chat_id, SkillCall("calendar.list", {"text": text}), handler)


async def _create_event(chat_id: int, text: str) -> str:
    # Extraction runs BEFORE the confirm gate, and the parsed fields go
    # into the stashed SkillCall args — so what the user confirms is
    # byte-identical to what runs. Re-extracting on "yes" could produce a
    # different time than the one shown (model nondeterminism).
    extracted = _structured_call(text, _EXTRACT_EVENT_SYSTEM.format(now=local_now().isoformat()))
    def clean_iso(value: str) -> str:
        # _parse_when gives the same protections events as reminders get
        # (naive -> local tz, model's spurious Z -> local wall time), and
        # microsecond junk from the model (seen live: 15:00:00.000123) is
        # noise, never intent.
        return scheduler._parse_when(str(value)).replace(microsecond=0).isoformat()

    try:
        data = json.loads(router.strip_code_fence(extracted.text))
        args = {
            "title": str(data["title"]),
            "start": clean_iso(_anchor_clock_time(text, str(data["start_iso"]))),
            "end": clean_iso(data["end_iso"]),
        }
        location = data.get("location")
        # Models sometimes emit the STRING "null" instead of JSON null —
        # seen live as an event 'at null'.
        if location and str(location).strip().lower() not in ("null", "none"):
            args["location"] = str(location)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        log_event("event_extraction_failed", text=text, raw=extracted.text, error=str(exc))
        return "I couldn't work out the event details — try e.g. \"add a meeting with Suman tomorrow 5pm to my calendar\"."

    # A fabricated or mistyped PAST event dies before the confirm ask —
    # walkthrough v3 (degraded mode): "book a flight to delhi" misrouted
    # into an ask for "Delhi Trip, Jan 2024".
    from datetime import timedelta as _td
    if scheduler._parse_when(args["start"]) < local_now() - _td(minutes=5):
        return (
            f"That start time ({humanize(args['start'])}) is in the past — "
            "tell me a future time for the event."
        )

    async def handler(handler_args: dict) -> str:
        try:
            created = await kernel.run_tool(kernel.ToolCall("calendar.create_event", handler_args))
        except kernel.ToolFailed as exc:
            return f"Couldn't create the event: {exc}"
        link = f"\n{created['link']}" if created.get("link") else ""
        return f"Event created on your calendar: \"{created['title']}\" at {humanize(handler_args['start'])}{link}"

    where = f" at {args['location']}" if args.get("location") else ""
    start_h = humanize(args["start"])
    same_day = scheduler._parse_when(args["start"]).date() == scheduler._parse_when(args["end"]).date()
    end_dt = scheduler._parse_when(args["end"])
    end_h = end_dt.strftime("%I:%M %p").lstrip("0") if same_day else humanize(args["end"])
    describe = f"About to create a calendar event: \"{args['title']}\" {start_h} → {end_h}{where}"
    return await _gated(chat_id, SkillCall("calendar.create", args), handler, describe=describe)


# v1 home scope: the bedroom AC plug (owner's decision). More devices =
# more entries here + the allowlist in permissions.yaml, nothing else.
_AC_SWITCH = "switch.ac"
_AC_POWER = "sensor.ac_current_consumption"
_AC_TODAY = "sensor.ac_today_s_consumption"
_TEMP = "sensor.bed_room_temp_temperature"
_HUMIDITY = "sensor.bed_room_temp_humidity"


def _since(last_changed: str | None) -> str:
    """'for 2h 05m' from HA's last_changed — '' when unknown."""
    if not last_changed:
        return ""
    try:
        from datetime import datetime

        delta = local_now() - datetime.fromisoformat(last_changed)
    except ValueError:
        return ""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return " for under a minute"
    if minutes < 60:
        return f" for {minutes}m"
    return f" for {minutes // 60}h {minutes % 60:02d}m"


# Rooms Kyraan knows it has NO sensor in — an honest "no sensor there"
# beats answering a kitchen question with bedroom data (seen live).
_UNSENSORED_ROOMS = ("living", "kitchen", "hall", "bathroom", "balcony", "dining", "office")


async def _check_email(chat_id: int, text: str = "") -> str:
    # "Open"/"read the email" asks for the body — which Kyraan deliberately
    # never fetches (§3a: metadata only). Say the boundary instead of
    # dumping the same list again (seen live: "can you open email?" got an
    # identical unread summary, as if it answered the question).
    wants_body = any(w in text.lower() for w in (
        "open", "read", "body", "content", "full", "detail", "more about",
        "tell me about", "about the email", "what does", "says", "summar",
    ))

    async def handler(_args: dict) -> str:
        # The reply the user sees carries senders/subjects; the history
        # records only a placeholder, so none of it reaches cloud models.
        _history_redaction.set("[showed the unread email summary]")
        try:
            result = await kernel.run_tool(kernel.ToolCall("email.unread", {"limit": 5}))
        except kernel.ToolFailed as exc:
            return f"Couldn't check email: {exc}"
        total = result.get("unread_estimate", 0)
        messages = result.get("messages", [])
        if not messages:
            return "No unread emails."
        lines = []
        if wants_body:
            lines.append(
                "I can't open email contents — by design I only see senders and "
                "subjects, never bodies (your data boundary). Open Gmail for the "
                "full message. Latest unread:"
            )
        else:
            lines.append(f"You have about {total} unread. Latest:")
        for m in messages:
            sender = m["from"].split("<")[0].strip().strip('"') or m["from"]
            lines.append(f"- {sender}: {m['subject']}")
        return "\n".join(lines)

    return await _gated(chat_id, SkillCall("email.check", {}), handler)


async def _home_query(chat_id: int, text: str) -> str:
    # Deterministic sub-routing on the classifier's cleaned text — device
    # answers stay template-composed, no model between the sensor and the
    # user. The question decides which card(s) to show.
    t = text.lower()
    wants_climate = any(w in t for w in ("temp", "humid", "hot", "warm", "cold", "climate"))
    wants_ac = "ac" in t.split() or any(w in t for w in ("power", "consum", "electric", "running", "watt", "plug"))
    other_room = next((r for r in _UNSENSORED_ROOMS if r in t), None)
    if not wants_climate and not wants_ac:
        wants_climate = wants_ac = True  # generic "home status" — show both

    async def handler(_args: dict) -> str:
        lines = []
        if wants_ac:
            try:
                state = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": _AC_SWITCH}))
                since = _since(state.get("last_changed"))
                if state["state"] != "on":
                    lines.append(f"The AC is OFF{since}.")
                else:
                    detail = ""
                    try:
                        power = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": _AC_POWER}))
                        today = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": _AC_TODAY}))
                        detail = f" — drawing {power['state']} {power['unit'] or 'W'}, {today['state']} {today['unit'] or 'kWh'} today"
                    except kernel.ToolFailed:
                        pass  # the on/off answer stands even if the sensors hiccup
                    lines.append(f"The AC is ON{since}{detail}.")
            except kernel.ToolFailed as exc:
                lines.append(f"Couldn't check the AC: {exc}")
        if wants_climate:
            prefix = ""
            if other_room:
                prefix = f"There's no sensor in the {other_room} room yet — the only climate sensor is in the bedroom. "
            try:
                temp = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": _TEMP}))
                humidity = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": _HUMIDITY}))
                lines.append(
                    f"{prefix}Bedroom: {temp['state']}{temp['unit'] or '°C'} / "
                    f"{humidity['state']}{humidity['unit'] or '%'} humidity."
                )
            except kernel.ToolFailed as exc:
                lines.append(f"{prefix}Couldn't read the bedroom sensor: {exc}")
        return "\n".join(lines)

    return await _gated(chat_id, SkillCall("home.query", {}), handler)


async def _home_control(chat_id: int, text: str) -> str:
    # Direction is decided deterministically from the normalized text —
    # a physical switch must never flip on a model's guess. "off" checked
    # first: "turn off" contains no "on", but "on" appears inside many
    # words, so an explicit standalone-word match is used for both.
    words = text.lower().replace(",", " ").split()
    if "off" in words:
        tool, verb = "home.turn_off", "OFF"
    elif "on" in words:
        tool, verb = "home.turn_on", "ON"
    else:
        return "Should the AC go on or off? Say e.g. \"turn off the AC\"."

    async def handler(args: dict) -> str:
        try:
            result = await kernel.run_tool(kernel.ToolCall(tool, args))
        except kernel.ToolFailed as exc:
            return f"Couldn't switch the AC: {exc}"
        # Read-back truth, not assumption: report what the plug says now —
        # and when HA's state hasn't converged (adapter polled and gave
        # up), say so honestly instead of reporting the stale value as
        # fact (seen live: confirmed ON, reply said OFF).
        if result.get("converged") is False:
            return (
                f"I sent the {verb} command, but the plug still reports "
                f"{result['state'].upper()} — give it a few seconds, then ask \"is the AC on?\" to verify."
            )
        return f"Done — the AC is now {result['state'].upper()}."

    describe = f"About to turn the AC {verb}"
    return await _gated(chat_id, SkillCall("home.control", {"entity": _AC_SWITCH}), handler, describe=describe)


async def _answer(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        # cheap, backed by llama3.1:8b as of 2026-08-25 — asked "what time
        # is it?" with the correct current time given directly in the
        # system prompt, the earlier 3B model (llama3.2) answered wrong in
        # 3/3 tries (14:40, 17:30, 15:50 — actual was 13:50); llama3.1:8b
        # was exactly right in 3/3, matching frontier. See
        # config/permissions.yaml's model_tiers comment.
        # Tier comes from config — moved to frontier 2026-08-25 evening:
        # chat.jsonl showed the local 8B collapsing on multi-turn
        # continuations ("make 2 paragraphs" -> garbled time-talk) and
        # contradicting the capability brief ("Yes, I have internet
        # access") — instruction-following over a now-large system prompt
        # is exactly where it's weakest (Ollama's default context also
        # truncates big prompts silently).
        tier = kernel.config.skill_config("qa.answer")["model_tier"]
        system = _ANSWER_SYSTEM.format(
            now=local_now().isoformat(),
            capabilities=capability_brief(),
            facts=memory_store.load_all_facts() or "(no facts stored yet)",
            pending_facts=memory_store.load_pending_facts() or "(none)",
            history=_history_block(chat_id),
        )
        try:
            response = router.call(prompt=args["text"], system=system, tier=tier)
        except router.ModelProviderError as exc:
            # Same degradation as intent classification: a frontier outage
            # (seen live: Groq's free 200k-token/day cap exhausted) drops
            # to the local model instead of failing the conversation.
            if tier == "cheap":
                raise
            log_event("qa_fallback_cheap", error=str(exc))
            response = router.call(prompt=args["text"], system=system, tier="cheap")
        return response.text

    return await _gated(chat_id, SkillCall("qa.answer", {"text": text}), handler)
