"""Single orchestrator for Phase 1 — no agent router yet (that's Phase 3).

Flow: normalize intent -> gate + dispatch through the kernel -> return text
for the Response Engine (here, just the Telegram send call) to deliver.
"""
import contextvars
import json
import time
from collections import defaultdict, deque

from kyraan.agents.capabilities import capability_brief
from kyraan.agents.guards import (  # noqa: F401 — re-exported names
    _GREETING_WORDS, _HOME_WORDS_EXACT, _HOME_WORD_STEMS, _LEADING_OPEN,
    _META_COMPLAINT_MARKERS, _META_DEMONSTRATIVES, _META_STARTERS,
    _META_YOU, _REMIND_WORDS, _TIME_WORDS, _TRAILING_OPEN, _is_greeting,
    _is_meta_question, _is_review_request, _mentions_home,
    is_time_fragment, thought_open,
)
from kyraan.agents.prompts import (  # noqa: F401
    _ANSWER_SYSTEM, _BURST_PLAN_SYSTEM, _EXTRACT_EVENT_SYSTEM,
    _EXTRACT_WHEN_SYSTEM, _EXTRACT_WINDOW_SYSTEM,
)
from kyraan.agents.review import (  # noqa: F401
    _load_review_proposals, _parse_review_decision,
)
from kyraan.control_plane import kernel
from kyraan.control_plane.kernel import ConfirmationRequired, KillSwitchEngaged, SkillCall
from kyraan.control_plane.logging_setup import log_chat, log_event
from kyraan.intent.normalize import normalize
from kyraan.memory import extraction
from kyraan.memory import store as memory_store
from kyraan.model_router import router
from kyraan.triggers import scheduler

# Confirm-first flow state: chat_id -> (SkillCall, handler) awaiting a
# yes/no. In-memory only — a restart drops any pending confirmation, which
# fails safe (the action just doesn't run). Phase 1 has no `confirm` skill
# wired into a live intent yet, but the kernel raises ConfirmationRequired
# for any skill the config marks `confirm` (including unlisted ones, which
# default to it), so the path for the user to say "yes" must exist before
# Phase 2 adds tools that rely on it.
_pending_confirmations: dict = {}
# Per-chat nonce for the CURRENT pending confirmation — inline buttons
# embed it, so a stale "Yes" from an older message can never confirm a
# newer action (security round P1).
_confirmation_nonce: dict = {}
# A pending confirmation goes stale: "About to turn the AC ON" asked at
# noon must not execute on an unrelated "yes" hours later. Physical
# actions deserve freshness.
_CONFIRMATION_TTL_S = 300
_CONFIRM_WORDS = {"yes", "y", "confirm", "ok", "okay", "do it", "go ahead"}

import re as _re

_FORGET_FACE_RE = _re.compile(
    r"^\s*forget\s+(?:the\s+)?face\s+(?:of\s+)?(.{2,40}?)\s*[.!]?\s*$",
    _re.IGNORECASE)
_DENY_WORDS = {"no", "n", "cancel", "don't", "dont", "stop"}

# Session state (history window, rolling summaries, transcript seeding)
# lives in session.py — re-exported so orchestrator remains the address
# tests and callers use; the dict objects are shared, so mutation-style
# monkeypatching keeps working.
from kyraan.agents.session import (  # noqa: F401
    _HISTORY_MAX_ENTRIES, _history, _summary_backlog, _SUMMARY_CHUNK,
    _summaries_path, _load_summaries, session_summary, _roll_summary,
    _legacy_cloud_placeholder, seed_history_from_log, record_exchange,
    record_proactive, _history_block, _classifier_context,
)

# Below this length a message can't state a durable fact ("yes", "hi",
# "thanks") — skip the extraction model call entirely.
_EXTRACTION_MIN_CHARS = 8

# Data boundary: a skill can replace what the conversation history records
# for its reply. Email summaries use this — sender/subject lines must not
# ride the history into the cloud classifier or qa prompts (owner's §3a
# resolution: work email metadata stays off third-party models).
_history_redaction: contextvars.ContextVar = contextvars.ContextVar("history_redaction", default=None)
_skip_extraction: contextvars.ContextVar = contextvars.ContextVar("skip_extraction", default=False)


# In-chat memory review: chat_id -> (proposals, stashed_at). Born live
# 2026-08-26: the owner said "reviewed and confirmed" and Kyraan claimed
# "I'll mark the remaining items as saved now" — a false action claim; the
# review gate only existed as a desktop CLI. The owner-only chat is as
# legitimate a place to review as the terminal, so the flow lives here
# too: list the pending facts, take approve/reject deterministically.
_pending_reviews: dict = {}
# A dropped (unconfirmed) ask must not vanish silently — the next reply
# says so (live: the owner got the ask, said "task list" instead of yes,
# and then wondered why the list was empty).
_dropped_ask_note: dict = {}

# The model-driven loop is the primary path in production; the classifier
# tests flip this off to exercise the fallback path in isolation.
AGENT_LOOP_ENABLED = True


async def _review_memory(chat_id: int, text: str) -> str:
    # A queue command states no facts — running extraction on "yes save
    # it" appended a bogus couldn't-distill warning under the review list
    # itself (live). And the listing carries UNAPPROVED proposal bodies:
    # history stores a placeholder so they never ride to the cloud on the
    # next request (security round 4, P1).
    _skip_extraction.set(True)
    _history_redaction.set("[showed the pending-review list]")

    async def handler(args: dict) -> str:
        proposals = _load_review_proposals()
        if not proposals:
            _pending_reviews.pop(chat_id, None)
            return ("Nothing is pending review — every fact you've approved is "
                    "already saved. To hear what I know, just ask — e.g. "
                    "\"what do you know about Aarav?\"")
        _pending_reviews[chat_id] = (proposals, time.monotonic())
        lines = [f"{i + 1}. {fact}  ({target})"
                 for i, (_, target, fact) in enumerate(proposals)]
        return ("Facts awaiting your review:\n" + "\n".join(lines) +
                "\n\nReply \"approve all\", \"approve 1,3\", \"reject 2\", or a mix "
                "(\"approve 1 reject 2\"). Anything else leaves them pending.")

    return await _gated(chat_id, SkillCall("memory.review", {"text": text}), handler)


def _cloud_tier_in_use() -> bool:
    """A tier is local only if its ENDPOINT is local — judged by
    router.provider_is_local, the same resolution routing itself uses
    (security round 2, P2)."""
    cfg = kernel.config.load()
    return any(not router.provider_is_local(t.get("provider", ""))
               for t in cfg.get("model_tiers", {}).values())


# The exact last reply each chat received (unredacted — _history may hold
# a redacted placeholder when a cloud tier is active) and when this
# process sent it (monotonic; absent after a restart, which is exactly
# right — seeded history must never look like a live exchange).
_last_sent_reply: dict = {}
_last_reply_at: dict = {}

async def _read_or_meta(chat_id: int, raw_text: str, intent: str, reply: str) -> str:
    """Deterministic backstop behind the classifier: a read-intent reply
    identical to the previous reply, triggered by a meta-question, means
    the classifier re-ran a tool the user was asking ABOUT — answer the
    question instead."""
    last = _last_sent_reply.get(chat_id, "").strip()
    # Containment, not equality: the re-run reply may carry a prefix (the
    # wants-body boundary line) around the same listing — live: "these
    # emails are already shared by u" got the boundary text PLUS the same
    # five emails again.
    same = bool(last) and reply.strip() and (reply.strip() in last or last in reply.strip())
    if same and _is_meta_question(raw_text):
        log_event("meta_question_rerouted", chat_id=chat_id, intent=intent, text=raw_text)
        return await _answer(chat_id, raw_text)
    return reply


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


async def _structured_call(prompt: str, system: str):
    """Structured extraction (reminder times, event fields, calendar
    windows) is exactness-critical: frontier first, local fallback when
    the cloud tier is down/exhausted — walkthrough v3 (degraded mode)
    showed the local 8B extracting \"in 45mins\" as a PAST time and
    failing window JSON outright."""
    try:
        return await router.acall(prompt=prompt, system=system, tier="frontier", force_json=True)
    except router.ModelProviderError as exc:
        log_event("structured_fallback_cheap", error=str(exc))
        return await router.acall(prompt=prompt, system=system, tier="cheap", force_json=True)


_SAVE_WORDS = ("remember", "save", "note that", "note this", "note it",
               "keep in mind", "make a note")


async def _extraction_note(chat_id: int, raw_text: str) -> str:
    """Run fact extraction and return a reply suffix naming what was queued
    ("" when nothing was). Extraction is best-effort: it must never break
    or replace the actual reply, so every failure is logged and swallowed.

    Exception to the silence: an EXPLICIT save request ("save the aarav
    age") that extracts nothing must say so — seen live: the save command
    dead-ended with 'Nothing is pending review' while the fact was never
    queued at all."""
    if len(raw_text.strip()) < _EXTRACTION_MIN_CHARS:
        return ""
    explicit_save = any(w in raw_text.lower() for w in _SAVE_WORDS)
    try:
        queued = await extraction.propose_from_message(
            raw_text, context=_classifier_context(chat_id), insist=explicit_save)
    except Exception as exc:
        log_event("extraction_error", error=str(exc), error_type=type(exc).__name__)
        return ""
    if not queued:
        if explicit_save:
            # An empty result may mean DEDUP, not failure — live: "i said
            # the age of aarav, you shoul save" warned 'couldn't distill'
            # while the fact was sitting in the queue already. Check both
            # stores before claiming failure.
            content = {w.strip(".,!?'\"").lower() for w in raw_text.split() if len(w) > 3}
            content -= {"save", "remember", "note", "that", "this", "should",
                        "shoul", "said", "please", "want", "need"}
            pending = memory_store.load_pending_facts().lower()
            live = memory_store.load_all_facts().lower()
            if content and any(w in pending for w in content):
                return ("\n\n📝 That's already in the review queue — say "
                        "\"review memory\" to approve it.")
            if content and any(w in live for w in content):
                return "\n\n📝 I already have that saved in memory."
            log_event("explicit_save_extracted_nothing", chat_id=chat_id, text=raw_text)
            return ("\n\n⚠️ I couldn't distill a durable fact from that to queue "
                    "for review — state it directly, like \"remember that Aarav "
                    "was born in October 2025\".")
        return ""
    facts = "; ".join(f.lstrip("- ").strip() for f in queued)
    return f"\n\n📝 Noted for review: {facts}"


class BurstSuperseded(Exception):
    """A new fragment arrived while a burst reply was still being planned —
    the draft is stale and NO action has run yet, so the channel retracts
    it and re-plans with the full thought. The human move: you're typing a
    reply, a new message lands, you stop, read it, and rethink."""


async def handle_burst(chat_id: int, texts: list, superseded=None) -> list:
    """Evaluate a burst TOGETHER, act on the minimal set of requests it
    contains, and ALWAYS answer with ONE composed reply — a human never
    sends five messages back (seen live: a 5-fragment casual burst got 5
    scattered replies). Returns [(quote_index, reply)] — a single tuple.

    `superseded` (an asyncio.Event the channel sets when another fragment
    arrives) is checked before anything with side effects runs: stale
    during planning -> BurstSuperseded. Once execution starts the reply is
    finished regardless — late fragments become the next burst."""
    def _stale() -> bool:
        return superseded is not None and superseded.is_set()

    texts = [t for t in texts if t]
    if not texts:
        return [(0, "")]
    if _stale():
        raise BurstSuperseded
    if len(texts) == 1:
        return [(0, await handle_message(chat_id, texts[0]))]

    if AGENT_LOOP_ENABLED:
        # G-03: the agent loop reads the whole conversation and handles a
        # multi-part message natively — the separate frontier planning
        # call (and its per-request loop runs) doubled latency for
        # nothing. The joined burst goes through as ONE message; the
        # planner below remains only for the classifier fallback path.
        joined = "\n".join(texts)
        log_event("burst_joined_for_agent", chat_id=chat_id, n=len(texts))
        return [(len(texts) - 1, await handle_message(chat_id, joined))]

    requests = ["\n".join(texts)]  # last-resort: treat as one merged message
    # Deterministic pre-verdict: all complete questions = distinct asks,
    # no model needed (rate-limit-proof).
    if all(t.rstrip().endswith("?") for t in texts):
        requests = list(texts)
        log_event("burst_plan", chat_id=chat_id, n=len(texts), mode="questions_heuristic")
    else:
        try:
            numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(texts))
            system = _BURST_PLAN_SYSTEM.format(n=len(texts), numbered=numbered)
            try:
                raw = await router.acall(prompt="Plan the requests.", system=system,
                                          tier="frontier", force_json=True)
            except router.ModelProviderError:
                raw = await router.acall(prompt="Plan the requests.", system=system,
                                         tier="cheap", force_json=True)
            candidate = json.loads(router.strip_code_fence(raw.text))
            planned = [str(r) for r in candidate.get("requests", []) if str(r).strip()]
            if 0 < len(planned) <= len(texts):
                requests = planned
                log_event("burst_plan", chat_id=chat_id, n=len(texts), requests=len(planned))
            else:
                raise ValueError("empty or oversized plan")
        except Exception as exc:
            log_event("burst_plan_fallback", chat_id=chat_id, error=str(exc))

    if _stale():
        # Planning took long enough for another fragment to land — the
        # plan no longer covers the whole thought. Nothing ran yet;
        # retract and let the channel re-plan with everything.
        log_event("burst_superseded", chat_id=chat_id, n=len(texts))
        raise BurstSuperseded

    replies = []
    for request in requests:
        replies.append(await handle_message(chat_id, request))
    combined = "\n\n".join(r for r in replies if r)
    return [(len(texts) - 1, combined)]


async def handle_message(chat_id: int, raw_text: str) -> str:
    from kyraan.control_plane.logging_setup import log_trace, new_turn
    new_turn()  # correlates every event/trace of this flow under one id
    turn_started = time.monotonic()
    log_trace("turn_start", chat_id=chat_id, user_text=raw_text)
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
    dropped = _dropped_ask_note.pop(chat_id, None)
    if dropped:
        reply = (f'(The earlier "{dropped}" ask was never confirmed, so I '
                 "dropped it — nothing was done. Ask again if you still want it.)"
                 f"\n\n{reply}")
    redacted = _history_redaction.get()
    for entry in (("user", raw_text), ("assistant", redacted or reply)):
        if len(_history[chat_id]) == _HISTORY_MAX_ENTRIES:
            # the oldest entry is about to fall off the window — keep it
            # for the rolling summary instead of losing it (harness C)
            _summary_backlog[chat_id].append(_history[chat_id][0])
        _history[chat_id].append(entry)
    await _roll_summary(chat_id)
    _history_redaction.reset(redaction_token)
    _last_sent_reply[chat_id] = reply
    _last_reply_at[chat_id] = time.monotonic()
    log_chat(chat_id, "user", raw_text)
    # The full reply stays in the LOCAL log (inside the §3a boundary);
    # cloud_text is what history seeding may hand back to cloud prompts —
    # without it, the redaction died at the first restart (review P1).
    log_chat(chat_id, "assistant", reply,
             **({"cloud_text": redacted} if redacted else {}))
    log_trace("turn_end", chat_id=chat_id, reply=reply,
              total_ms=round((time.monotonic() - turn_started) * 1000))
    return reply


async def _dispatch(chat_id: int, raw_text: str) -> str:
    try:
        # Deterministic fragment patience — no model consulted: a bare
        # time-phrase can't be a complete request, and the classifier was
        # seen inventing a reminder out of one.
        if is_time_fragment(raw_text) and chat_id not in _pending_confirmations:
            _skip_extraction.set(True)
            return "Go on — I'm listening…"
        pending = _pending_confirmations.pop(chat_id, None)
        if pending:
            _confirmation_nonce.pop(chat_id, None)
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
                try:
                    return str(await kernel.run_skill(call, handler))
                except ConfirmationRequired:
                    # A confirmed replay that re-raises the gate (a nested
                    # action wanting its own yes, or pre-fix code that
                    # dropped the flag — seen live 2026-08-26 as a generic
                    # "Something went wrong" on a gated reminder) must
                    # never fall to the catch-all: re-stash and ask
                    # honestly instead of reporting a phantom failure.
                    log_event("confirmation_replay_regated", skill=call.skill_name)
                    _pending_confirmations[chat_id] = (call, handler, time.monotonic())
                    return (f"'{call.skill_name}' still needs a confirmation "
                            'step — reply "yes" again to proceed, or "no" to cancel.')
            elif word in _DENY_WORDS:
                log_event("confirmation_denied", skill=call.skill_name)
                return f"Okay — '{call.skill_name}' cancelled, nothing was done."
            else:
                # Anything else: the user moved on. Drop the pending action
                # (fail safe, never run it implicitly) and handle the new
                # message normally.
                log_event("confirmation_dropped", skill=call.skill_name)
                _dropped_ask_note[chat_id] = call.skill_name

        review = _pending_reviews.get(chat_id)
        if review:
            proposals, stashed_at = review
            if time.monotonic() - stashed_at > _CONFIRMATION_TTL_S:
                _pending_reviews.pop(chat_id, None)  # stale — fall through
            else:
                decision = _parse_review_decision(raw_text, len(proposals))
                if decision is None:
                    # The user moved on — facts stay pending, never
                    # implicitly approved.
                    _pending_reviews.pop(chat_id, None)
                else:
                    _pending_reviews.pop(chat_id, None)
                    _skip_extraction.set(True)
                    approved_idx, rejected_idx = decision

                    async def _apply_review(_a: dict) -> str:
                        # Runs INSIDE the kernel (security round P1: the
                        # decision used to promote/reject with the kill
                        # switch engaged) — kill switch, audit, the works.
                        saved, discarded = [], []
                        for i in approved_idx:
                            path, target, fact = proposals[i]
                            if path.exists():
                                memory_store.promote(path)
                                saved.append(fact)
                                log_event("memory_promoted_via_chat", target=target, fact=fact[:80])
                        for i in rejected_idx:
                            path, target, fact = proposals[i]
                            if path.exists():
                                memory_store.reject(path)
                                discarded.append(fact)
                                log_event("memory_rejected_via_chat", target=target, fact=fact[:80])
                        remaining = len(_load_review_proposals())
                        parts = []
                        if saved:
                            parts.append("✅ Saved to memory: " + "; ".join(saved))
                        if discarded:
                            parts.append("🗑 Rejected: " + "; ".join(discarded))
                        if remaining:
                            parts.append(f"{remaining} still pending — say \"review memory\" to see them.")
                        return "\n".join(parts) if parts else "Nothing changed."

                    _history_redaction.set("[applied the owner's review decisions]")
                    return str(await kernel.run_skill(
                        SkillCall("memory.review", {"approve": approved_idx,
                                                    "reject": rejected_idx}, confirmed=True),
                        _apply_review))

        # The model-driven tool loop is the PRIMARY brain (2026-08-26): a
        # frontier model reads the conversation + memory + tool menu and
        # decides — chaining reads, composing its own replies, hitting the
        # same kernel gates for every tool. The classifier path below is
        # its fallback: degraded mode (cloud down) or any loop failure.
        word = raw_text.strip().lower().rstrip(".!")
        if word in ("yes", "no"):
            last_assistant = next((t for role, t in reversed(_history[chat_id])
                                   if role == "assistant"), "")
            if 'reply "yes"' in last_assistant:
                # G-05: the last reply WAS a confirm ask, but nothing is
                # pending (checked above) — the ask died with a restart,
                # since pending confirmations live in process memory.
                # Landing this yes/no in the agent loop produced confusing
                # improvisation; honesty first. Conversational assent
                # ("go ahead", a yes to a model's own question) falls
                # through to the loop as normal conversation.
                log_event("orphaned_confirmation_word", chat_id=chat_id, word=word)
                return ("That ask didn't survive a restart, so nothing is "
                        "waiting for your yes/no — please repeat the request "
                        "and I'll ask again.")

        forget_face = _FORGET_FACE_RE.match(raw_text)
        if forget_face:
            # Deterministic, like the review phrases: deleting a biometric
            # must never depend on a model's routing choice.
            from kyraan.agents import faces as _faces
            wanted = forget_face.group(1).strip()

            async def _forget_face(_a: dict) -> str:
                if _faces.forget(wanted):
                    return f'Deleted the stored face template for "{wanted}".'
                known = _faces.enrolled_names()
                return (f'No stored face named "{wanted}".'
                        + (f" Enrolled: {', '.join(known)}" if known else
                           " No faces are enrolled."))

            _skip_extraction.set(True)
            return await _gated(
                chat_id, SkillCall("faces.forget", {"name": wanted}), _forget_face,
                describe=f'About to DELETE the stored face template for "{wanted}"')

        if _is_review_request(raw_text):
            # Deterministic: the review flow OWNS these phrases. Routed
            # through the agent loop, "review memory" listed the queue via
            # a read-only tool and the owner's "approve all" then had no
            # session to act on (live) — the approval path must never
            # depend on a model's routing choice.
            return await _review_memory(chat_id, raw_text)

        if AGENT_LOOP_ENABLED:
            # ONE brain, two tiers (G-02): the same agent loop runs on the
            # frontier, then on the local cheap tier when the cloud is
            # unreachable — degraded mode no longer means a different
            # system, just a smaller model behind the same doctrine. The
            # legacy classifier below survives only as the third line, for
            # the case where the local model can't even hold the loop's
            # decision JSON.
            from kyraan.agents import agent_loop
            for tier in ("frontier", "cheap"):
                try:
                    return await agent_loop.run(chat_id, raw_text, tier=tier)
                except KillSwitchEngaged:
                    raise
                except agent_loop.AgentUnavailable as exc:
                    log_event("agent_tier_fallback", tier=tier, error=str(exc)[:200])
                except Exception as exc:
                    log_event("agent_loop_error", tier=tier, error=str(exc),
                              error_type=type(exc).__name__)
            log_event("agent_fallback_classifier")

        # Structured JSON intent classification needs more reliability than
        # the cheap tier's local 3B model consistently gives — verified
        # live (2026-08-25): the cheap tier misclassified a clear reminder
        # request ("set reminder in 5mis 'Call to MIra'" got routed to
        # reminders.list) and missed simple questions like "what time is
        # it?"/"who are you?", while frontier (still free, via Groq) was
        # 14/14 correct across the same test set. Classification is a tiny,
        # fast call even on the bigger model — there's no real cost to
        # always using it here, so this isn't a cheap-first-then-escalate
        # dance anymore, just the reliable tier directly.
        context = _classifier_context(chat_id)
        try:
            import asyncio as _aio
            parsed = await _aio.to_thread(normalize, raw_text, tier="frontier", history=context)
        except router.ModelProviderError as exc:
            # Frontier (Groq) is classification's single cloud dependency —
            # if it's down or rate-limited, degrade to the local cheap tier
            # (measured at 12-13/14 on the same test set) instead of
            # refusing to understand anything at all.
            log_event("intent_fallback_cheap", error=str(exc))
            parsed = await _aio.to_thread(normalize, raw_text, tier="cheap", history=context)

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
            if parsed.intent not in ("qa.answer", "unknown", "incomplete"):
                # The intent came from the same hallucinated reading the
                # rewrite did — seen live: "on my smoke havite" rewrote to
                # "what's the status of my humidifier" (rejected) but the
                # home.query intent survived and dumped the AC status.
                # Discredit both together; conversation is the safe path.
                log_event("intent_demoted_after_rejected_rewrite",
                          chat_id=chat_id, intent=parsed.intent)
                parsed.intent = "qa.answer"
        log_event("intent_classified", chat_id=chat_id, intent=parsed.intent,
                  confidence=parsed.confidence, normalized=parsed.normalized_text)
        if parsed.confidence < 0.4:
            _skip_extraction.set(True)  # a message we couldn't parse can't state reliable facts
            return "I'm not confident I understood that — could you rephrase?"

        if parsed.intent == "incomplete":
            # A human doesn't answer half a sentence — they wait for the
            # rest. Seen live: "tomorrow morning" (sent a minute before
            # "remind me at 9") got an irrelevant morning-brief answer.
            # Deterministic, no model call; the fragment stays in history
            # so the next message completes the thought.
            _skip_extraction.set(True)
            return "Go on — I'm listening…"
        if parsed.intent == "reminders.create":
            wording = f"{raw_text} {parsed.normalized_text}".lower()
            if not any(w in wording for w in _REMIND_WORDS):
                # Seen live: "to buy something" — a fragment of a STORY
                # about the user's morning — became a junk midnight
                # reminder. Nobody asks for a reminder without remind-ish
                # wording; without it the classifier is over-reaching, so
                # treat the message as conversation instead.
                log_event("reminder_intent_demoted", chat_id=chat_id, text=raw_text)
                return await _answer(chat_id, parsed.normalized_text)
            return await _create_reminder(chat_id, parsed.normalized_text)
        if parsed.intent == "reminders.list":
            return await _read_or_meta(chat_id, raw_text, parsed.intent,
                                       await _list_reminders(chat_id))
        if parsed.intent == "reminders.cancel":
            return await _cancel_reminder(chat_id, parsed.normalized_text)
        if parsed.intent == "calendar.list":
            return await _read_or_meta(chat_id, raw_text, parsed.intent,
                                       await _list_calendar(chat_id, parsed.normalized_text))
        if parsed.intent == "calendar.create":
            return await _create_event(chat_id, parsed.normalized_text)
        if parsed.intent == "calendar.cancel":
            return await _cancel_event(chat_id, parsed.normalized_text)
        if parsed.intent == "email.check":
            return await _read_or_meta(chat_id, raw_text, parsed.intent,
                                       await _check_email(chat_id, parsed.normalized_text))
        if parsed.intent == "memory.review":
            return await _review_memory(chat_id, parsed.normalized_text)
        if parsed.intent == "home.query":
            wording = f"{raw_text} {parsed.normalized_text}"
            if not _mentions_home(wording):
                # Same over-reach family as the reminder guard: a home
                # status question mentions the home somehow ("AC",
                # "temperature", "bedroom", ...). Without any such word
                # ("on my smoke havite", seen live answered with the full
                # AC/climate dump) the classifier is guessing — converse.
                log_event("home_query_demoted", chat_id=chat_id, text=raw_text)
                return await _answer(chat_id, parsed.normalized_text)
            return await _read_or_meta(chat_id, raw_text, parsed.intent,
                                       await _home_query(chat_id, parsed.normalized_text))
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
    except kernel.ToolFailed as exc:
        # ToolFailed messages are user-facing by contract (on_failure:
        # surface) — the catch-all was replacing "the command MAY still
        # have gone through" with a generic error, inviting exactly the
        # unsafe retry that warning exists to prevent (review P1).
        return f"That didn't complete: {exc}"
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
        import uuid as _uuid
        # The message just became a confirm-gated ACTION — it is a
        # command, not a fact, and must not ALSO land in the memory
        # review queue (live: scheduling a task filed "you want to check
        # tomorrow's calendar every evening" as a durable fact).
        _skip_extraction.set(True)
        _confirmation_nonce[chat_id] = _uuid.uuid4().hex[:12]
        _pending_confirmations[chat_id] = (call, handler, time.monotonic())
        what = describe or f"'{call.skill_name}' needs your confirmation first"
        return f"{what} — reply \"yes\" to confirm or \"no\" to cancel."




# Imported LAST (module bottom): legacy_handlers imports this module back,
# and by now every name it needs at call time exists. The handlers bind
# into THIS namespace as bare names so both _dispatch and the tests'
# monkeypatch.setattr(orchestrator, "_answer", ...) resolve through the
# same global.
from kyraan.agents.legacy_handlers import (  # noqa: E402,F401
    _answer, _cancel_event, _cancel_reminder, _check_email,
    _create_event, _create_reminder, _home_control, _home_query,
    _list_calendar, _list_reminders,
)
