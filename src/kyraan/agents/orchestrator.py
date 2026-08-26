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
from kyraan.control_plane.dnd import humanize, local_now
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


# In-chat memory review: chat_id -> (proposals, stashed_at). Born live
# 2026-08-26: the owner said "reviewed and confirmed" and Kyraan claimed
# "I'll mark the remaining items as saved now" — a false action claim; the
# review gate only existed as a desktop CLI. The owner-only chat is as
# legitimate a place to review as the terminal, so the flow lives here
# too: list the pending facts, take approve/reject deterministically.
_pending_reviews: dict = {}

# The model-driven loop is the primary path in production; the classifier
# tests flip this off to exercise the fallback path in isolation.
AGENT_LOOP_ENABLED = True


async def _review_memory(chat_id: int, text: str) -> str:
    # A queue command states no facts — running extraction on "yes save
    # it" appended a bogus couldn't-distill warning under the review list
    # itself (live).
    _skip_extraction.set(True)

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
    tiers = kernel.config.load().get("model_tiers", {})
    return any(t.get("provider") != "ollama" for t in tiers.values())


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


def _is_email_listing(text: str) -> bool:
    """The two legacy email-reply templates (metadata listings written to
    chat.jsonl before the cloud_text twin existed)."""
    import re
    return bool(re.search(r"You have about \d+ unread", text)
                or "Latest unread:" in text)


def seed_history_from_log(max_per_chat: int = 40) -> None:
    """Rebuild in-memory conversation history from chat.jsonl at startup.

    Found live 2026-08-26: five minutes after a service restart, 'are
    those the latest emails?' got a fabricated 'No, those are not the
    latest' — the restart had wiped _history, so qa was judging a listing
    it could not see. The log on disk has the whole conversation; a
    restart should be invisible to the user."""
    from kyraan.control_plane import logging_setup

    path = logging_setup.CHAT_LOG
    if not path.exists():
        return
    per_chat: dict = defaultdict(list)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        log_event("history_seed_failed", error=str(exc))
        return
    for line in lines[-2000:]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = entry.get("role")
        if role == "proactive":
            role = "assistant"
        if role in ("user", "assistant") and entry.get("text"):
            text = entry.get("cloud_text") or entry["text"]
            if role == "assistant" and "cloud_text" not in entry and _is_email_listing(text):
                # Pre-upgrade log entries carry the full listing with no
                # cloud twin (review P1) — the templates are fixed, so
                # legacy listings are recognized and redacted here.
                text = "[showed the unread email summary]"
            per_chat[entry["chat_id"]].append((role, text))
    for chat_id, entries in per_chat.items():
        if not _history[chat_id]:  # never clobber a live conversation
            _history[chat_id].extend(entries[-max_per_chat:])
    log_event("history_seeded", chats=len(per_chat))


def record_proactive(chat_id: int, text: str) -> None:
    """Proactive sends (reminders, briefs) belong in conversation history
    too — found live: \"Thanks for the reminder\" got \"I didn't actually
    send you any reminders\" because fire() bypassed _history entirely."""
    _history[chat_id].append(("assistant", text))
    log_chat(chat_id, "proactive", text)


def _history_block(chat_id: int, clip: int = 600, older_clip: int | None = None) -> str:
    """Per-entry clip: one pasted article must not drown the prompt (and
    with Ollama's default 4K context it literally truncated the system
    instructions — the likely cause of a live garbled reply).

    older_clip: tighter cap for everything but the last 8 entries — recent
    turns carry the follow-up context and stay at full clip; old turns keep
    their gist. Token thrift without dropping what's actually used."""
    entries = list(_history[chat_id])
    lines = []
    for i, (role, text) in enumerate(entries):
        cap = clip
        if older_clip is not None and i < len(entries) - 8:
            cap = older_clip
        lines.append(f"{role}: {text[:cap] + '…' if len(text) > cap else text}")
    return "\n".join(lines) or "(no conversation yet)"


def _classifier_context(chat_id: int, entries: int = 6, clip: int = 200) -> str:
    """Compact tail of the conversation for intent classification — enough
    to resolve a follow-up, clipped so a long calendar listing or code
    answer doesn't drown the classifier prompt."""
    recent = list(_history[chat_id])[-entries:]
    return "\n".join(
        f"{role}: {text[:clip] + '…' if len(text) > clip else text}" for role, text in recent
    )


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

    Exception to the silence: an EXPLICIT save request ("save the kiaan
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
            # the age of kiaan, you shoul save" warned 'couldn't distill'
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
    redacted = _history_redaction.get()
    _history[chat_id].append(("user", raw_text))
    _history[chat_id].append(("assistant", redacted or reply))
    _history_redaction.reset(redaction_token)
    _last_sent_reply[chat_id] = reply
    _last_reply_at[chat_id] = time.monotonic()
    log_chat(chat_id, "user", raw_text)
    # The full reply stays in the LOCAL log (inside the §3a boundary);
    # cloud_text is what history seeding may hand back to cloud prompts —
    # without it, the redaction died at the first restart (review P1).
    log_chat(chat_id, "assistant", reply,
             **({"cloud_text": redacted} if redacted else {}))
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
        # request ("set reminder in 5mis 'Call to RUma'" got routed to
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
        extracted = await _structured_call(text, _EXTRACT_WHEN_SYSTEM.format(now=local_now().isoformat()))
        try:
            data = json.loads(router.strip_code_fence(extracted.text))
            if is_time_fragment(str(data.get("text", ""))):
                # A reminder whose TEXT is itself just a time phrase is a
                # broken extraction ("tomorrow morning" at 6 AM, seen live).
                return "Remind you about what? Tell me the task and I'll set it."
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
        window = await _structured_call(args["text"], _EXTRACT_WINDOW_SYSTEM.format(now=local_now().isoformat()))
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


async def _cancel_event(chat_id: int, text: str) -> str:
    """Cancel calendar events. Targets are resolved BEFORE the confirm
    gate so the ask names exactly what will be removed — born from a live
    disaster: with no cancel capability, qa PROMISED cancellation twice
    and the classifier then created a junk event titled 'Cancel All
    Events' on the real calendar."""
    from datetime import timedelta

    window = await _structured_call(text, _EXTRACT_WINDOW_SYSTEM.format(now=local_now().isoformat()))
    try:
        data = json.loads(router.strip_code_fence(window.text))
        start, end = data["start_iso"], data["end_iso"]
        label = data.get("label") or "that period"
    except (json.JSONDecodeError, KeyError, TypeError):
        # No time phrase ("cancel the test event") is normal — search the
        # coming week.
        start = local_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end = (local_now() + timedelta(days=7)).isoformat()
        label = "the next 7 days"

    try:
        events = await kernel.run_tool(kernel.ToolCall("calendar.list_events", {"start": start, "end": end}))
    except kernel.ToolFailed as exc:
        return f"Couldn't check the calendar: {exc}"
    events = [e for e in events if e.get("id")]
    if not events:
        return f"Nothing on the calendar {label} to cancel."

    stop = {"cancel", "cancle", "delete", "remove", "the", "a", "an", "event",
            "events", "all", "my", "from", "calendar", "please", "meeting",
            "meetings", "appointment", "today", "tomorrow", "this", "that",
            "week", "yes", "right", "now", "it", "them", "and", "of", "for",
            "can", "you", "everything", "every"}
    words = {w.strip(".,!?\"'").lower() for w in text.split()}
    content = words - stop - {""}
    if content:
        targets = [e for e in events
                   if content & {w.strip(".,!?\"'").lower() for w in e["title"].split()}]
        if not targets:
            listing = "\n".join(f"- {humanize(e['start'])} — {e['title']}" for e in events[:8])
            return (f"I couldn't match that to an event {label}. On the calendar:\n"
                    f"{listing}\nWhich one should I cancel?")
    elif words & {"all", "everything", "every"}:
        targets = list(events)  # explicitly asked for everything in the window
    else:
        # Bare "can you cancel" with no object — a human asks which, never
        # defaults to sweeping the whole calendar (live 2026-08-26: it
        # escalated straight to a DELETE-4-events ask).
        listing = "\n".join(f"- {humanize(e['start'])} — {e['title']}" for e in events[:8])
        return (f"Cancel which event? On the calendar {label}:\n{listing}\n"
                "Name the one to cancel — or say \"cancel all events\" for all of them.")

    # Recurring occurrences share their series id — deleting it removes
    # the whole series, so collapse duplicates and say it out loud.
    seen, unique = set(), []
    for e in targets:
        if e["id"] not in seen:
            seen.add(e["id"])
            unique.append(e)
    overflow = 0
    if len(unique) > 8:
        # Never confirm more than the kernel's 8-call rail can actually
        # run (review P2: the ask covered the full batch, execution
        # stopped at eight). The remainder is named up front.
        overflow = len(unique) - 8
        unique = unique[:8]

    async def handler(args: dict) -> str:
        # Per-event isolation + a COMPLETE receipt: deleted /
        # outcome-unknown / untouched are three DIFFERENT truths — a
        # timed-out delete may have succeeded, and calling it "not
        # touched" invites a double-delete (round-5 P2).
        deleted, already_gone, untouched = [], [], []
        unknown = ""
        stop_reason = ""
        for i, e in enumerate(unique):
            try:
                result = await kernel.run_tool(kernel.ToolCall(
                    "calendar.delete_event", {"event_id": e["id"], "title": e["title"]}))
            except kernel.ToolFailed as exc:
                stop_reason = str(exc)
                if "MAY still have gone" in stop_reason:
                    unknown = e["title"]
                    untouched = [x["title"] for x in unique[i + 1:]]
                else:
                    untouched = [x["title"] for x in unique[i:]]
                break
            (already_gone if result.get("already_gone") else deleted).append(e["title"])
        parts = []
        if deleted:
            parts.append("Deleted from your calendar: " + ", ".join(f'"{t}"' for t in deleted))
        if already_gone:
            parts.append("Already gone: " + ", ".join(f'"{t}"' for t in already_gone))
        if unknown:
            parts.append(f'Outcome UNKNOWN for "{unknown}" — the delete timed out and may '
                         "have succeeded; check the calendar before retrying it")
        remaining = len(untouched) + overflow
        resume = f'say "cancel all events {label}" again'
        if untouched:
            parts.append(f"NOT touched ({stop_reason.split(':')[0]}): "
                         + ", ".join(f'"{t}"' for t in untouched))
        if remaining:
            # ALWAYS account for everything beyond what ran (round-6 P2:
            # overflow silently vanished whenever a batch stopped early),
            # and the resume phrase carries the ORIGINAL WINDOW so
            # "next month" doesn't resume as "today". Fresh listing on
            # the re-run stays the design: cached ids go stale, and
            # already-deleted events resolve harmlessly.
            parts.append(f"{remaining} event(s) still to cancel — {resume}")
        return ". ".join(parts) if parts else "Nothing was deleted."

    described = "\n".join(
        f"- {humanize(e['start'])} — {e['title']}"
        + (" (recurring — the WHOLE series will be removed)" if e.get("recurring") else "")
        for e in unique)
    describe = (f"About to DELETE {len(unique)} event(s) from your Google Calendar:\n"
                f"{described}\nThis can't be undone from here")
    if overflow:
        describe += (f"\n({overflow} more matched — this batch is capped at 8; "
                     "run \"cancel all events\" again afterwards for the rest)")
    return await _gated(chat_id, SkillCall("calendar.cancel", {"text": text}), handler, describe=describe)


async def _create_event(chat_id: int, text: str) -> str:
    # Extraction runs BEFORE the confirm gate, and the parsed fields go
    # into the stashed SkillCall args — so what the user confirms is
    # byte-identical to what runs. Re-extracting on "yes" could produce a
    # different time than the one shown (model nondeterminism).
    extracted = await _structured_call(text, _EXTRACT_EVENT_SYSTEM.format(now=local_now().isoformat()))
    def clean_iso(value: str) -> str:
        # _parse_when gives the same protections events as reminders get
        # (naive -> local tz, model's spurious Z -> local wall time), and
        # microsecond junk from the model (seen live: 15:00:00.000123) is
        # noise, never intent.
        return scheduler._parse_when(str(value)).replace(microsecond=0).isoformat()

    try:
        data = json.loads(router.strip_code_fence(extracted.text))
        # ONE time normalization shared with the agent loop (round-6 P2:
        # this path had drifted behind — no tolerance on the anchor, no
        # end>start check): guards.normalized_event_times does sanitize,
        # tolerant anchoring against the user's words, and range sanity.
        from kyraan.agents.guards import normalized_event_times
        start_iso, end_iso = normalized_event_times(
            {"start": str(data["start_iso"]), "end": str(data["end_iso"])}, text)
        args = {
            "title": str(data["title"]),
            "start": clean_iso(start_iso),
            "end": clean_iso(end_iso),
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
        # The reply the user sees carries senders/subjects; when any model
        # tier is a CLOUD provider the history records only a placeholder,
        # so none of it reaches third parties. With local-only tiers
        # (2026-08-26) redaction is pure capability loss — qa couldn't see
        # the listing the user's follow-up ("are these latest emails?")
        # was asking about — so the real text stays in history.
        if _cloud_tier_in_use():
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
    # A control intent without any device mention is a misroute — seen
    # live: "let me fix you" became "Should the AC go on or off?". No
    # device word, no switch talk: answer conversationally instead.
    device_words = {"ac", "plug", "switch", "socket", "appliance"}
    if not (device_words & {w.strip(".,!?") for w in text.lower().split()}):
        return await _answer(chat_id, text)
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
        from kyraan.memory import engine
        system = _ANSWER_SYSTEM.format(
            now=local_now().isoformat(),
            capabilities=capability_brief(),
            facts=engine.memory_context(args["text"]),
            pending_facts=memory_store.load_pending_facts() or "(none)",
            history=_history_block(chat_id),
        )
        try:
            response = await router.acall(prompt=args["text"], system=system, tier=tier)
        except router.ModelProviderError as exc:
            # Same degradation as intent classification: a frontier outage
            # (seen live: Groq's free 200k-token/day cap exhausted) drops
            # to the local model instead of failing the conversation.
            if tier == "cheap":
                raise
            log_event("qa_fallback_cheap", error=str(exc))
            # Degraded-mode self-awareness — live transcript: the user said
            # "you are confused / randomly answering" while the fallback
            # model spiraled, and Kyraan never admitted its state.
            system += (
                "\n\nIMPORTANT: you are currently running on the smaller "
                "LOCAL backup model because the main model is rate-limited. "
                "Keep replies short and factual. If the user says you seem "
                "confused, wrong, or repetitive, tell them honestly: the main "
                "model is temporarily rate-limited and reply quality is "
                "reduced for a few minutes — don't argue or deflect."
            )
            response = await router.acall(prompt=args["text"], system=system, tier="cheap")
            tier = "cheap"
        reply = response.text
        recent = [t.strip() for role, t in list(_history[chat_id])[-6:] if role == "assistant"]
        # A pathological loop repeats within MINUTES to different
        # questions; greeting a greeting identically hours later is just
        # being human. Found live: after history seeding, "helo" the next
        # morning matched last night's greeting reply and got the
        # I'm-repeating-myself apology. The guard needs a live exchange
        # this process (< 15 min) and never fires on a greeting.
        recently_active = time.monotonic() - _last_reply_at.get(chat_id, float("-inf")) < 900
        if (reply.strip() and reply.strip() in recent and recently_active
                and not _is_greeting(args["text"])):
            # A human never sends the same sentence twice in a row —
            # verbatim repetition is a small-model failure mode (seen
            # live 2026-08-26: "I can't book cabs yet." to three
            # different questions). One retry with the problem named;
            # if it STILL repeats, admit it instead of looping.
            log_event("qa_repetition_detected", chat_id=chat_id, reply=reply[:80])
            retry = await router.acall(prompt=args["text"], system=system + (
                "\n\nIMPORTANT: your previous draft repeated one of your own "
                "earlier replies word-for-word. Answer THIS message "
                "specifically; do not reuse any earlier sentence."), tier=tier)
            if retry.text.strip() and retry.text.strip() not in recent:
                return retry.text
            return ("I'm repeating myself — sorry, I didn't process that "
                    "properly. Could you say it another way?")
        return reply

    return await _gated(chat_id, SkillCall("qa.answer", {"text": text}), handler)
