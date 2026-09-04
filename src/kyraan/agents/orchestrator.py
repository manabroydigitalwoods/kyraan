"""The orchestrator: deterministic guards -> confirm flow -> the agent
loop (frontier, then local cheap tier) -> honest outage reply. One
brain, two tiers, zero dispatch rules — the classify-and-dispatch
architecture retired 2026-08-27 (P3.7b) after the cheap-tier loop
passed the full HARD eval twice consecutively.
"""
import contextvars
import contextvars as _contextvars
import json

from kyraan.control_plane.dnd import local_now
import re
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
# A bare acknowledgment deserves a bare acknowledgment. Round five of
# the menu disease (2026-08-27): a lone "ok" reached the loop and came
# back as "Okay 👍 Anything else you want me to check (bedroom temp,
# AC status...)?" — a menu question laundered as an ack. Deterministic
# branch, zero model calls, nothing to extract.
_ACK_WORDS = {"ok", "okay", "k", "kk", "thanks", "thank you", "thx", "ty",
              "got it", "nice", "cool", "great", "perfect", "👍", "🙏", "❤️",
              "ok thanks", "okay thanks", "thik ache", "thik", "acha"}

import re as _re

_CORRECTION_RE = _re.compile(
    r"^\s*(?:no[,.! ]|that'?s (?:wrong|not right|not it)|wrong[,.! ]"
    r"|i meant\b|i said\b|not that\b|it (?:will|should) be\b"
    r"|actually[,. ]|you (?:got|read) (?:it|that) wrong)",
    _re.IGNORECASE)

_FORGET_FACE_RE = _re.compile(
    r"^\s*forget\s+(?:the\s+)?face\s+(?:of\s+)?(.{2,40}?)\s*[.!]?\s*$",
    _re.IGNORECASE)
_DENY_WORDS = {"no", "n", "cancel", "don't", "dont", "stop"}
# P3.1c: bare "undo" or a targeted "undo the reminder/task/event/...".
_CONSOLIDATE_RE = _re.compile(
    r"^\s*(?:(?:please\s+)?(?:consolidate|dedupe?|de-dupe|deduplicate)"
    r"|remove\s+duplicate|clean\s*up\s+(?:my\s+)?duplicate)"
    r"\s*(?:my\s+)?(?:duplicate\s+)?memor(?:y|ies)\s*[.!?]?\s*$",
    _re.IGNORECASE)
_UNDO_RE = _re.compile(
    r"^\s*undo(?:\s+(?:the\s+|that\s+|my\s+|last\s+)*"
    r"(reminder|task|event|meeting|switch|light|ac|plug|face)s?)?"
    r"\s*[.!]?\s*$", _re.IGNORECASE)
_UNDO_TARGETS = {"reminder": "reminders.", "task": "tasks.",
                 "event": "calendar.", "meeting": "calendar.",
                 "switch": "home.", "light": "home.", "ac": "home.",
                 "plug": "home.", "face": "faces."}

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
# P3.7a: this turn fell to the cheap tier — the SAME local model then
# serves extraction, so its cutoff must absorb the loop's tail.
_degraded_turn: contextvars.ContextVar = contextvars.ContextVar("degraded_turn", default=False)


def _extraction_timeout(explicit_save: bool) -> int:
    if explicit_save:
        return 45
    return 30 if _degraded_turn.get() else 6


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

def _proposal_hint(path) -> str:
    """" — replaces: …" / " — similar to: …" from the proposal's meta, so
    the owner sees what an approval does to the store (2026-09-04)."""
    try:
        import json as _j
        from pathlib import Path as _P
        text = _P(path).read_text()
        line = next((ln for ln in text.splitlines() if ln.startswith("meta:")), "")
        meta = _j.loads(line.split("meta:", 1)[1].strip()) if line else {}
    except Exception:
        return ""
    if meta.get("supersedes"):
        return f"\n   ↳ replaces: {str(meta['supersedes'])[:90]}"
    if meta.get("similar_to"):
        return f"\n   ↳ similar to (kept): {str(meta['similar_to'])[:90]}"
    return ""


async def _review_memory(chat_id: int, text: str) -> str:
    # A queue command states no facts — running extraction on "yes save
    # it" appended a bogus couldn't-distill warning under the review list
    # itself (live). And the listing carries UNAPPROVED proposal bodies:
    # history stores a placeholder so they never ride to the cloud on the
    # next request (security round 4, P1).
    _skip_extraction.set(True)
    _history_redaction.set("[showed the pending-review list]")

    async def handler(args: dict) -> str:
        proposals = _load_review_proposals(kernel.viewer_person())
        if not proposals:
            _pending_reviews.pop(chat_id, None)
            return ("Nothing is pending review — every fact you've approved is "
                    "already saved. To hear what I know, just ask — e.g. "
                    "\"what do you know about Aarav?\"")
        _pending_reviews[chat_id] = (proposals, time.monotonic())
        lines = []
        for i, (path, target, fact) in enumerate(proposals):
            hint = _proposal_hint(path)
            lines.append(f"{i + 1}. {fact}  ({target}){hint}")
        return ("Facts awaiting your review:\n" + "\n".join(lines) +
                "\n\nReply \"approve all\", \"approve 1,3\", \"reject 2\", or a mix "
                "(\"approve 1 reject 2\"). Anything else leaves them pending.")

    return await _gated(chat_id, SkillCall("memory.review", {"text": text}), handler)


def _describe_last_turn(chat_id: int) -> str:
    """Owner phrase "show last turn": termination, tools, cost, timing
    of this chat's previous turn — from telemetry, zero model calls."""
    import json as _json

    from kyraan.control_plane import logging_setup as _logs
    last = None
    try:
        for line in _logs.TRACE_LOG.read_text().splitlines():
            if '"turn_end"' in line:
                try:
                    e = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if e.get("chat_id") == chat_id:
                    last = e
    except OSError:
        pass
    if last is None:
        return "No completed turn on record for this chat yet."
    tid = last.get("turn_id")
    tools, cost, calls = [], 0.0, 0
    try:
        for line in _logs.EVENT_LOG.read_text().splitlines():
            if tid and tid in line:
                try:
                    ev = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if ev.get("kind") == "agent_tool_call":
                    tools.append(ev.get("tool", "?"))
                elif ev.get("kind") == "model_call":
                    calls += 1
                    cost += ev.get("cost_usd") or 0
    except OSError:
        pass
    lines = ["🔍 Last turn:"]
    lines.append(f"- Ended: {last.get('termination', 'unknown')}")
    lines.append(f"- Model calls: {calls} — ${cost:.4f}")
    if tools:
        lines.append("- Tools: " + " → ".join(tools[:8]))
    if last.get("total_ms"):
        lines.append(f"- Took: {last['total_ms'] / 1000:.1f}s")
    reply_preview = str(last.get("reply") or "")[:120]
    if reply_preview:
        lines.append(f'- Replied: "{reply_preview}…"')
    return "\n".join(lines)


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
        # "10.15 pm" is a dotted-minute time, common in Indian English —
        # the old pattern read its ".15 pm" as "15 pm" and rewrote the
        # reminder to 15:00 (review 2026-09-03). The lookbehind refuses
        # a digit run that continues a number; the dot joins the minutes.
        _CLOCK_RE = re.compile(r"(?<![\d.:])(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b", re.I)
    matches = _CLOCK_RE.findall(raw_text)
    if len(matches) != 1:
        return when_iso
    hh, mm, ap = matches[0]
    hour = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    minute = int(mm) if mm else 0
    if not (0 <= int(hh) <= 12 and minute < 60):
        return when_iso
    parsed = scheduler._parse_when(when_iso)
    if (parsed.hour, parsed.minute) == (hour, minute):
        return when_iso
    # Only a NEAR miss is an extraction slip worth overriding; a stated
    # time hours away from the model's is another event's boundary or a
    # date the model resolved differently — leave it to the confirm gate.
    stated_min, model_min = hour * 60 + minute, parsed.hour * 60 + parsed.minute
    if min(abs(stated_min - model_min), 1440 - abs(stated_min - model_min)) > 90:
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

# An explicit save is an INSTRUCTION to store something, not any sentence
# containing a save-word: "How can I save time?" and "do you remember my
# birthday?" matched the raw substrings and inherited explicit-save
# behavior — including the patient 45s extraction ceiling, delaying an
# ordinary reply (Bugbot P2 round 4). The save-verb must carry an object
# ("remember THAT/THIS/MY..."), questions are never save commands, and
# "you remember/save" is about Kyraan's memory, not a new fact.
_EXPLICIT_SAVE_RE = re.compile(
    r"\b(?:remember|save|note)\s+\S"
    r"|\bkeep in mind\b|\bmake a note\b|\bmemori[sz]e\b",
    re.IGNORECASE)

# A determiner in front turns the verb into a NOUN — "this note contains
# the recipe" is prose, not an instruction (Bugbot P1 round 5).
_SAVE_AS_NOUN_RE = re.compile(
    r"\b(?:a|an|the|this|that|these|those|my|your|his|her|our|their|"
    r"some|any|each|every|no)\s+(?:note|save)s?\b", re.IGNORECASE)

# "can/could you remember that X" is a POLITE COMMAND — it was being
# rejected with the recall questions (Bugbot P1 round 5). Checked before
# the non-command shapes so a polite save containing "to save" survives.
_POLITE_SAVE_RE = re.compile(
    r"\b(?:can|could|please|pls|kindly)\s+(?:you\s+)?(?:also\s+)?"
    r"(?:remember|save|note)\s+\S",
    re.IGNORECASE)

# The non-command shapes: a recall auxiliary before "you save/remember"
# asks about Kyraan's memory ("do you remember...?"); a first-person or
# infinitive construction is the USER saving something themselves ("I
# need to save money") — while "you should save tarun name" (live owner
# phrasing) commands Kyraan and must pass.
_SAVE_NONCOMMAND_RE = re.compile(
    r"\b(?:do|did|does|will|would|won'?t|don'?t|didn'?t|shall)\s+"
    r"you\s+(?:remember|save|note)\b"
    r"|\b(?:i|we)\s+(?:\w+\s+){0,2}(?:save|remember|note)\b"
    r"|\bto\s+(?:save|remember|note)\b",
    re.IGNORECASE)


def is_explicit_save(text: str) -> bool:
    """True when the message INSTRUCTS Kyraan to store something. Wrong
    either way costs: a false positive holds an ordinary reply behind the
    patient 45s extraction ceiling; a false negative silently drops a
    fact the owner asked for."""
    stripped = text.strip()
    if stripped.endswith("?"):
        return False
    if _POLITE_SAVE_RE.search(stripped):
        return True
    if _SAVE_NONCOMMAND_RE.search(stripped):
        return False
    if not _EXPLICIT_SAVE_RE.search(stripped):
        return False
    # Only a noun usage in the whole message -> prose, not a command.
    without_nouns = _SAVE_AS_NOUN_RE.sub(" ", stripped)
    return bool(_EXPLICIT_SAVE_RE.search(without_nouns))


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
    explicit_save = is_explicit_save(raw_text)
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
        if "?" not in raw_text:
            # P3.7a: a STATEMENT that extracted nothing may be a fact we
            # already hold (the model skips what the conversation already
            # knows — qwen especially). Deterministic check, honest note:
            # silence read as "not saved" in the degraded eval.
            # ...but a 2-word overlap is not "already saved" (live: "i have
            # mentioned 5 minutes but still you are confused" got the note).
            from kyraan.memory import engine as _engine
            asked = _engine._words(raw_text)
            if any(asked <= _engine._words(m["content"]) or len(asked & _engine._words(m["content"])) >= 4
                   for m in _engine.find_matches(raw_text)):
                return "\n\n📝 I already have that saved in memory."
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

    # G-03: the agent loop reads the whole conversation and handles a
    # multi-part message natively — the joined burst goes through as ONE
    # message (the classifier-era planner retired with P3.7b).
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


def _photo_just_sent(chat_id: int, max_age_s: int = 20 * 60) -> bool:
    """The after-photo rails ("that is ruma", "this is my medicine",
    "links?") need a photo to have JUST been sent — in the last two
    user entries of this chat, and recently (audit 2026-09-03: "that is
    ruma" answering "who is picking him up?" twenty minutes after any
    photo would have linked her to it)."""
    users = [t for role, t in list(_history[chat_id])[-4:] if role == "user"]
    if not any(str(t).startswith("[sent a photo") for t in users[-2:]):
        return False
    from kyraan.store import documents as _d
    cap = _d.latest_capture(chat_id, max_age_h=1)
    return cap is not None and (time.time() - cap["created"].timestamp()) <= max_age_s


_secret_turn: _contextvars.ContextVar = _contextvars.ContextVar("secret_turn", default=False)
_user_redaction: _contextvars.ContextVar = _contextvars.ContextVar("user_redaction", default=None)


last_owner_turn_at: float = 0.0   # for the prompt-cache keep-warm (2026-09-04)


async def handle_message(chat_id: int, raw_text: str) -> str:
    global last_owner_turn_at
    if kernel.viewer_stage() == "owner":
        last_owner_turn_at = time.time()
    from kyraan.control_plane.logging_setup import (
        log_trace, new_turn, start_anomaly_capture)
    new_turn()  # correlates every event/trace of this flow under one id
    start_anomaly_capture()  # health layer: this turn's anomaly verdict
    turn_started = time.monotonic()
    from kyraan.agents import secrets as _secrets
    from kyraan.control_plane.logging_setup import set_trace_redaction, reset_trace_redaction
    is_private = kernel.viewer_person() == "owner" and (
        _secrets.opens(raw_text) or _secrets.active(chat_id)
        or _secrets.private_command(raw_text) is not None)
    # the trace log must never hold a private turn's text — decided
    # before the first trace line of the turn is written
    trace_token = set_trace_redaction(_secrets.PLACEHOLDER if is_private else None)
    try:
        return await _handle_message_traced(chat_id, raw_text, turn_started, is_private)
    finally:
        reset_trace_redaction(trace_token)


def tier_chain() -> tuple:
    """The model tiers to try, in order, from config: frontier, then a
    standby cloud tier when one is configured, then the local model."""
    try:
        from kyraan.control_plane import config as _config
        tiers = _config.load().get("model_tiers") or {}
    except Exception:
        tiers = {"frontier": {}, "cheap": {}}
    return tuple(t for t in ("frontier", "standby", "cheap") if t in tiers)


async def _greeting_reply(chat_id: int) -> str:
    """Time of day, then what is next or what is waiting — one line."""
    from datetime import timedelta as _td
    now = local_now()
    hour = now.hour
    hello = ("Morning." if 5 <= hour < 12 else "Afternoon." if hour < 17
             else "Evening." if hour < 22 else "Late one.")
    glance = ""
    try:
        end = now.replace(hour=23, minute=59, second=0, microsecond=0)
        events = await kernel.run_tool(kernel.ToolCall(
            "calendar.list_events", {"start": now.isoformat(), "end": end.isoformat()}), meta=True)
        nxt = next((e for e in (events or []) if not e.get("all_day")), None)
        if nxt:
            from datetime import datetime as _dt
            try:
                at = _dt.fromisoformat(str(nxt["start"])).strftime("%I:%M %p").lstrip("0")
            except ValueError:
                at = str(nxt["start"])[11:16]
            glance = f"Next: {nxt['title']} at {at}."
    except Exception:
        pass
    try:
        from kyraan.triggers import chief_of_staff as _cos_g
        waiting = await _cos_g.needs_reply_lines(chat_id)
        if waiting:
            glance = (glance + " " if glance else "") + (
                f"{len(waiting)} thing{'s' if len(waiting) != 1 else ''} waiting — say \"what's open\".")
    except Exception:
        pass
    if not glance:
        glance = "Nothing waiting."
    return f"{hello} {glance}"


_last_processing: dict = {}
_pending_suggestion: dict = {}   # chat_id -> (phrase, monotonic) — "did you mean"
_background_tasks: set = set()


def processing_marker(chat_id: int) -> str:
    """Where the last reply was made (owner 2026-09-03: "show where
    every reply was processed"): 🔒 this Mac, ☁️ the cloud."""
    return "🔒 on this Mac" if _last_processing.get(chat_id, "local") == "local" else "☁️ via cloud"


_answered_by: _contextvars.ContextVar = _contextvars.ContextVar("answered_by", default="local")


async def _handle_message_traced(chat_id: int, raw_text: str, turn_started: float,
                                 is_private: bool) -> str:
    from kyraan.control_plane.logging_setup import log_trace
    from kyraan.agents import secrets as _secrets
    log_trace("turn_start", chat_id=chat_id, user_text=raw_text)
    if _CORRECTION_RE.match(raw_text):
        # Eval-candidate capture (audit item 5, 2026-08-28): a turn the
        # user opens by CORRECTING the previous reply is the highest-
        # value eval material there is — logged with the reply it
        # corrects, greppable as a corpus for the golden suite.
        last = next((t for role, t in reversed(_history[chat_id])
                     if role == "assistant"), "")
        log_event("user_correction_candidate", chat_id=chat_id,
                  correction=raw_text[:200], corrected_reply=last[:300])
    redaction_token = _history_redaction.set(None)
    skip_token = _skip_extraction.set(False)
    degraded_token = _degraded_turn.set(False)
    secret_token = _secret_turn.set(False)
    user_red_token = _user_redaction.set(None)
    answered_token = _answered_by.set("local")
    cmd = _secrets.private_command(raw_text) if kernel.viewer_person() == "owner" else None
    if cmd is not None:
        _secrets.set_private(chat_id, cmd == "on")
        if cmd == "off":
            _secrets.close(chat_id)
        _skip_extraction.set(True)
        log_event("private_mode", chat_id=chat_id, on=cmd == "on")
        reply = ("🔒 Private mode ON — everything stays on this Mac until you "
                 "say \"private mode off\". Nothing from here is remembered."
                 if cmd == "on" else
                 "Private mode OFF — back to normal (cloud model, memory on).")
        _history_redaction.set(reply)
        return _finish_turn(chat_id, raw_text, reply, turn_started,
                            redaction_token, skip_token, degraded_token,
                            secret_token, user_red_token, answered_token)
    if is_private:
        # SECRET WINDOW (owner 2026-09-03: "isko secret rakho" — see
        # agents/secrets.py). This turn never reaches the cloud tier,
        # never enters memory, and leaves only a placeholder behind in
        # anything a later cloud prompt could read.
        if _secrets.retro(raw_text):
            _secrets.redact_recent(chat_id)
        _secret_turn.set(True)
        _skip_extraction.set(True)
        _user_redaction.set(_secrets.PLACEHOLDER)
        _history_redaction.set(_secrets.PLACEHOLDER)
        if _secrets.closes(raw_text):
            _secrets.close(chat_id)
        else:
            _secrets.touch(chat_id)
        log_event("secret_turn", chat_id=chat_id,
                  retro=_secrets.retro(raw_text), closed=_secrets.closes(raw_text))
    reply = await _dispatch(chat_id, raw_text)
    return await _finish_turn_async(chat_id, raw_text, reply, turn_started,
                                    redaction_token, skip_token, degraded_token,
                                    secret_token, user_red_token, answered_token)


def _finish_turn(chat_id, raw_text, reply, turn_started, redaction_token, skip_token,
                 degraded_token, secret_token, user_red_token, answered_token):
    """Record + trace a turn that needed no model (a switch flip)."""
    _skip_extraction.reset(skip_token)
    _degraded_turn.reset(degraded_token)
    redacted = _history_redaction.get()
    user_red = _user_redaction.get()
    for entry in (("user", user_red or raw_text), ("assistant", redacted or reply)):
        _history[chat_id].append(entry)
    _history_redaction.reset(redaction_token)
    _secret_turn.reset(secret_token)
    _user_redaction.reset(user_red_token)
    _last_processing[chat_id] = _answered_by.get()
    _answered_by.reset(answered_token)
    _last_sent_reply[chat_id] = reply
    _last_reply_at[chat_id] = time.monotonic()
    log_chat(chat_id, "user", raw_text, **({"cloud_text": user_red} if user_red else {}))
    log_chat(chat_id, "assistant", reply, **({"cloud_text": redacted} if redacted else {}))
    from kyraan.control_plane.logging_setup import log_trace, turn_summary
    log_trace("turn_end", chat_id=chat_id, reply=reply, termination="deterministic",
              total_ms=round((time.monotonic() - turn_started) * 1000), **turn_summary())
    return reply


async def _finish_turn_async(chat_id, raw_text, reply, turn_started, redaction_token,
                             skip_token, degraded_token, secret_token, user_red_token,
                             answered_token):
        from kyraan.control_plane.logging_setup import log_trace
        if kernel.viewer_person() != "owner":
            # P3.5c first-month rule: extraction from a non-owner's messages
            # only with their per-person opt-in flag; proposals then route to
            # THEIR review queue via the reviewer stamp in propose_fact.
            from kyraan.store import persons as _persons
            if not _persons.extraction_enabled(kernel.viewer_person()):
                _skip_extraction.set(True)
        if not _skip_extraction.get():
            import asyncio as _aio

            from kyraan.control_plane.logging_setup import stage as _stage
            # Extraction is bookkeeping — it must never hold the reply
            # hostage. Live 2026-08-27: a local-model reload made "ok
            # that great" take 23s because extraction sat on the reply
            # path. A slow turn skips extraction for ONE message.
            # EXCEPT an explicit "remember/save this": there extraction IS
            # the request — a 6s cutoff silently swallowed it during cold
            # model reloads (Bugbot P1), so it waits out a full Ollama
            # reload, and if even that expires it says so instead of
            # pretending the fact was noted.
            explicit_save = is_explicit_save(raw_text)
            try:
                with _stage("extraction"):
                    reply += await _aio.wait_for(
                        _extraction_note(chat_id, raw_text),
                        timeout=_extraction_timeout(explicit_save))
            except _aio.TimeoutError:
                log_event("extraction_skipped_slow", chat_id=chat_id,
                          explicit_save=explicit_save)
                if explicit_save:
                    reply += ("\n\n(I couldn't queue that for memory just now — "
                              "the local model is too slow to respond. Nothing "
                              "was saved; tell me again in a minute.)")
        _skip_extraction.reset(skip_token)
        was_degraded = _degraded_turn.get()   # read BEFORE the reset (review 2026-09-03)
        _degraded_turn.reset(degraded_token)
        # Health layer (2026-08-27): the turn's verdict — one event with the
        # anomaly kinds this turn saw and its latency; a crossed threshold
        # appends ONE in-band warning line (throttled per kind per day).
        from kyraan.control_plane.logging_setup import collected_anomalies
        anomalies = collected_anomalies()
        log_event("turn_health", chat_id=chat_id,
                  anomalies=sorted(set(anomalies)) or None,
                  anomaly_count=len(anomalies),
                  latency_ms=round((time.monotonic() - turn_started) * 1000),
                  degraded=was_degraded or None)
        if kernel.viewer_person() == "owner":
            # Warning lights are OWNER-ONLY: a non-owner turn's anomalies
            # still land in events and the nightly census, but the in-band
            # line must never surface system internals in someone else's
            # chat — nor burn the daily alert where the owner can't see it.
            try:
                from kyraan.triggers import health_alerts
                alert = health_alerts.check(anomalies)
                if alert:
                    reply += alert
            except Exception as exc:  # the light must never break the reply
                log_event("health_alert_failed", error=str(exc)[:120])

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
        user_red = _user_redaction.get()
        for entry in (("user", user_red or raw_text), ("assistant", redacted or reply)):
            if len(_history[chat_id]) == _HISTORY_MAX_ENTRIES:
                # the oldest entry is about to fall off the window — keep it
                # for the rolling summary instead of losing it (harness C)
                _summary_backlog[chat_id].append(_history[chat_id][0])
            _history[chat_id].append(entry)
        # Summary rolling is equally off-path: fire-and-forget — its own
        # error handling re-queues the backlog chunk on failure.
        import asyncio as _aio2
        _task = _aio2.create_task(_roll_summary(chat_id))
        _background_tasks.add(_task)          # the loop holds tasks weakly
        _task.add_done_callback(_background_tasks.discard)
        _history_redaction.reset(redaction_token)
        _secret_turn.reset(secret_token)
        _user_redaction.reset(user_red_token)
        _last_processing[chat_id] = _answered_by.get()
        _answered_by.reset(answered_token)
        _last_sent_reply[chat_id] = reply
        _last_reply_at[chat_id] = time.monotonic()
        log_chat(chat_id, "user", raw_text,
                 **({"cloud_text": user_red} if user_red else {}))
        # The full reply stays in the LOCAL log (inside the §3a boundary);
        # cloud_text is what history seeding may hand back to cloud prompts —
        # without it, the redaction died at the first restart (review P1).
        log_chat(chat_id, "assistant", reply,
                 **({"cloud_text": redacted} if redacted else {}))
        from kyraan.control_plane.logging_setup import turn_summary
        from kyraan.agents import agent_loop as _al
        log_trace("turn_end", chat_id=chat_id, reply=reply,
                  termination=_al.termination(),
                  total_ms=round((time.monotonic() - turn_started) * 1000),
                  **turn_summary())
        return reply


async def _dispatch(chat_id: int, raw_text: str) -> str:
    try:
        # Deterministic fragment patience — no model consulted: a bare
        # time-phrase can't be a complete request, and the classifier was
        # seen inventing a reminder out of one. But a fragment ANSWERING
        # a question Kyraan just asked ("what time on 30 Aug?" → "at
        # 5am") is a complete answer and must reach the loop — seen live
        # 2026-08-27: the guard swallowed it with "Go on — I'm
        # listening…" and the vaccination reminder was never created.
        # Recent turns are scanned (not just the newest) because a
        # proactive can land between the question and the answer.
        skip_m = _re.match(
            r"^\s*(?:(?:play\s+(?:the\s+)?)?(next|previous|prev|last)\s+(?:song|track|one)"
            r"|skip(?:\s+(?:this|the)\s+(?:song|track))?|next|go\s+back(?:\s+(?:a|one)\s+(?:song|track))?)"
            r"\s*[?!.]*\s*$", raw_text, _re.IGNORECASE)
        if skip_m and kernel.viewer_person() == "owner":
            # "next song" (live 2026-09-03 15:58: the loop said it could
            # not switch tracks on the Echo). Media transport is auto by
            # doctrine; deterministic here.
            from kyraan.agents import loop_tools as _lt_m
            from kyraan.tools import spotify as _sp_m
            if _sp_m.configured():
                word = (skip_m.group(1) or "next").lower()
                direction = "previous" if word.startswith(("prev", "last")) or "back" in raw_text.lower() else "next"
                _skip_extraction.set(True)
                try:
                    out = await _lt_m._music_skip(chat_id, {"direction": direction}, raw_text)
                except kernel.ToolFailed as exc:
                    return f"Couldn't skip: {exc}"
                if out.get("verified") is False:
                    return f"I sent {direction}, but Spotify still reports the same track — is something playing?"
                now = f' — now "{out["now"]}"' if out.get("now") else ""
                where = f' on {out["on"]}' if out.get("on") else ""
                return f"Skipped to the {direction} track{where}{now}."
        if is_time_fragment(raw_text) and chat_id not in _pending_confirmations:
            recent = [t for role, t in _history[chat_id]
                      if role == "assistant"][-3:]
            # "...? 🙂" ended with an emoji and the guard swallowed
            # "today" (live 2026-09-02): a question mark followed only by
            # non-word characters still ends a question. A bare small
            # number after an ENUMERATED list ("1) … 2) … 3) …") is the
            # owner picking one (review item, 2026-09-04).
            enumerated = any(_re.search(r"(?m)^\s*(?:\d[.)]|[•\-])\s+\S", t) for t in recent)
            picks = _re.fullmatch(r"\s*\d{1,2}\s*[.)]?\s*", raw_text) is not None
            if not any(_re.search(r"\?[\W_]*$", t) for t in recent) and not (enumerated and picks):
                _skip_extraction.set(True)
                return "Go on — I'm listening…"
        pending = _pending_confirmations.pop(chat_id, None)
        if pending is None:
            pending = _load_persisted_confirmation(chat_id)  # P3.4b: the
            # ask survived a restart — rebuild the byte-identical replay
        if pending:
            _confirmation_nonce.pop(chat_id, None)
            from kyraan.store import redis_kv as _rkv
            _rkv.delete(_rkv.key("confirm", chat_id))  # resolved either way
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
                    import uuid as _uuid_r
                    _confirmation_nonce[chat_id] = _uuid_r.uuid4().hex[:12]  # buttons need a live nonce
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
                                if str(target).startswith("persona/"):
                                    # A learned RULE, not a fact: the
                                    # owner's yes lands it in the persona
                                    # block, never the memory tree.
                                    from kyraan.memory import lessons
                                    rule = fact.split(":", 1)[-1].strip()
                                    lessons.apply(rule, [fact])
                                    path.unlink(missing_ok=True)
                                    saved.append(f"rule adopted — {rule}")
                                    continue
                                if memory_store.dispute_meta(path) is not None:
                                    # P3.5d: approving a dispute = the new
                                    # claim stands, under THIS reviewer's
                                    # authority
                                    outcome = memory_store.resolve_dispute(path, keep_new=True)
                                    saved.append(f"dispute resolved — {outcome}")
                                    log_event("dispute_resolved_via_chat",
                                              target=target, keep_new=True)
                                    continue
                                memory_store.promote(path)
                                saved.append(fact)
                                log_event("memory_promoted_via_chat", target=target, fact=fact[:80])
                        for i in rejected_idx:
                            path, target, fact = proposals[i]
                            if path.exists():
                                if str(target).startswith("persona/"):
                                    path.unlink(missing_ok=True)
                                    discarded.append(fact)
                                    log_event("lesson_rejected", fact=fact[:80])
                                    continue
                                if memory_store.dispute_meta(path) is not None:
                                    outcome = memory_store.resolve_dispute(path, keep_new=False)
                                    discarded.append(f"dispute resolved — {outcome}")
                                    log_event("dispute_resolved_via_chat",
                                              target=target, keep_new=False)
                                    continue
                                memory_store.reject(path)
                                discarded.append(fact)
                                log_event("memory_rejected_via_chat", target=target, fact=fact[:80])
                        remaining = len(_load_review_proposals(kernel.viewer_person()))
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
        from kyraan.agents import commands as _commands
        if _re.match(r"^\s*(?:help|commands?|what can i say|what can you do|list commands)\s*[?!.]*\s*$",
                     raw_text, _re.IGNORECASE):
            _skip_extraction.set(True)
            _history_redaction.set("[listed the exact commands]")
            return _commands.help_text()
        word = raw_text.strip().lower().rstrip(".!")
        pending_sugg = _pending_suggestion.get(chat_id)
        if (word in _CONFIRM_WORDS and pending_sugg
                and time.monotonic() - pending_sugg[1] < 300
                and chat_id not in _pending_confirmations):
            # "Did you mean 'reindex vault'?" -> "yes": run that phrase
            _pending_suggestion.pop(chat_id, None)
            log_event("command_suggestion_taken", chat_id=chat_id, phrase=pending_sugg[0])
            return await _dispatch(chat_id, pending_sugg[0])
        if word in _DENY_WORDS and pending_sugg:
            _pending_suggestion.pop(chat_id, None)
        if word in _CONFIRM_WORDS or word in _DENY_WORDS:
            # Scan the last few assistant turns, not just the newest: a
            # proactive (temp alert, reminder) landing between the ask
            # and the answer hid the ask and the owner's "ok" fell to
            # the loop as small talk (found live 2026-08-27, AC-on ask
            # killed by a deploy restart one minute after it was asked).
            # An ask FOLLOWED by a resolution reply ("Done — ...",
            # "cancelled") is settled — a casual "ok" after a completed
            # confirm must not trigger a false "that ask expired".
            recent = [t for role, t in _history[chat_id]
                      if role == "assistant"][-5:]
            ask_idx = max((i for i, t in enumerate(recent)
                           if 'reply "yes"' in t), default=-1)
            resolved = any(t.lstrip().startswith("Done")
                           or "cancel" in t.lower()
                           or "no longer pending" in t  # this very notice
                           for t in recent[ask_idx + 1:]) if ask_idx >= 0 \
                else True
            if ask_idx >= 0 and not resolved:
                # G-05: a recent reply WAS a confirm ask, but nothing is
                # pending (checked above) — the ask died with a restart
                # or its freshness TTL. Landing this word in the agent
                # loop produced confusing improvisation; honesty first.
                # Assent to a model's own conversational question still
                # falls through: those replies never contain the literal
                # 'reply "yes"' phrasing.
                log_event("orphaned_confirmation_word", chat_id=chat_id, word=word)
                return ("That ask is no longer pending (it expired or died "
                        "with a restart), so nothing is waiting for your "
                        f"\"{word}\" — please repeat the request and I'll "
                        "ask again.")

        if word in _ACK_WORDS:
            _skip_extraction.set(True)
            return "👍"

        forget_doc = _re.match(
            r"^\s*forget\s+(?:the\s+|that\s+)?(?:document|doc|card|pdf|brochure)"
            r"\s+(?:about\s+|of\s+)?(.{2,60}?)\s*[.!]?\s*$",
            raw_text, _re.IGNORECASE)
        if forget_doc:
            # Deterministic, like forget-face: destroying a capture must
            # never depend on a model's routing choice.
            _skip_extraction.set(True)
            return await _forget_document(chat_id, forget_doc.group(1).strip())

        forget_face = _FORGET_FACE_RE.match(raw_text)
        if forget_face and kernel.viewer_person() == "owner":   # the roster is biometric, owner-only
            # Deterministic, like the review phrases: deleting a biometric
            # must never depend on a model's routing choice.
            from kyraan.agents import faces as _faces
            wanted = forget_face.group(1).strip()
            known = _faces.enrolled_names()
            if not any(_faces._slug(wanted) == _faces._slug(n) for n in known):
                # Nothing to delete — asking the owner to confirm deleting
                # a face that doesn't exist is a nonsense gate (eval case
                # faces.forget.unknown caught the ask, 2026-08-27).
                _skip_extraction.set(True)
                return (f'No stored face named "{wanted}".'
                        + (f" Enrolled: {', '.join(known)}" if known else
                           " No faces are enrolled."))

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

        code_m = _re.match(r"^\s*(?:code|coding\s+task|dev\s+task)\s*:\s*(.{8,})$",
                           raw_text, _re.IGNORECASE | _re.DOTALL)
        code_cmd = _re.match(r"^\s*code\s+(status|diff|discard)(?:\s+(\S+))?\s*[?!.]*\s*$",
                             raw_text, _re.IGNORECASE)
        if (code_m or code_cmd) and kernel.viewer_person() == "owner":
            # "code: <task>" — a coding task for Claude Code on the repo
            # (owner 2026-09-03, option 2). Deterministic and confirm-
            # gated; "code status/diff/discard" read or drop the result.
            from kyraan.agents import loop_tools as _lt_code
            _skip_extraction.set(True)
            if code_m:
                task_text = code_m.group(1).strip()

                async def _start(_a: dict) -> str:
                    out = await _lt_code.TOOLS["code.task"]["run"](chat_id, {"task": task_text}, raw_text)
                    return _lt_code._confirmed_reply("code.task", {"task": task_text}, out)

                return await _gated(
                    chat_id, SkillCall("code.task", {"task": task_text}), _start,
                    describe=_lt_code._describe_call("code.task", {"task": task_text}, raw_text, chat_id))
            sub, job = code_cmd.group(1).lower(), (code_cmd.group(2) or "")
            if sub == "status":
                out = await _lt_code.TOOLS["code.status"]["run"](chat_id, {}, raw_text)
                last = out.get("last") if isinstance(out, dict) else None
                if not last:
                    return "No coding tasks yet. Start one with \"code: <what to change>\"."
                return (f"Latest coding task: {last['task'][:120]}\n{last['status']} — branch "
                        f"{last['branch']}" + (f", ${last['cost_usd']:.2f}" if last.get("cost_usd") else ""))
            if sub == "diff":
                out = await _lt_code.TOOLS["code.diff"]["run"](chat_id, {"job": job}, raw_text)
                _history_redaction.set("[showed a coding task's diff]")
                return f"Diff for {out['job']} ({out['branch']}, {out['chars']} chars):\n{out['diff']}"

            async def _drop(_a: dict) -> str:
                out = await _lt_code.TOOLS["code.discard"]["run"](chat_id, {"job": job}, raw_text)
                return _lt_code._confirmed_reply("code.discard", {"job": job}, out)

            return await _gated(chat_id, SkillCall("code.discard", {"job": job}), _drop,
                                describe=_lt_code._describe_call("code.discard", {"job": job}, raw_text, chat_id))
        keeper_status = _re.match(
            r"^\s*(?:kiaan(?:'s|’s)?\s+(?:status|vaccines?|vaccinations?|schedule|next\s+(?:vaccine|shot|dose))"
            r"|(?:when|what)\s+is\s+kiaan(?:'s|’s)?\s+next\s+(?:vaccine|vaccination|shot|dose)"
            r"|kiaan\s+milestones?"
            # any short ask that names Kiaan and vaccination (live 2026-09-04:
            # "kiaan's vaccination upcoming days" went to the model, which
            # asked "what year?")
            r"|(?=.*\bkiaan)(?=.*vaccin)[\w'’ ,?-]{6,60})\s*[?!.]*\s*$", raw_text, _re.IGNORECASE)
        keeper_mark = _re.match(
            r"^\s*kiaan\s+(?:got|had|took|received|has\s+(?:had|got|taken))\s+(?:the\s+|his\s+)?"
            r"(.{2,60}?)\s*(?:shot|dose|vaccine|vaccination|jab|injection)?\s*(?:today|yesterday|on\s+(\S+))?\s*[.!]*\s*$",
            raw_text, _re.IGNORECASE)
        keeper_skip = _re.match(
            r"^\s*kiaan\s+(?:skipped|skips|is\s+skipping|won'?t\s+(?:get|take|have)|doctor\s+skipped)\s+(?:the\s+)?"
            r"(.{2,60}?)\s*(?:shot|dose|vaccine|vaccination)?\s*[.!]*\s*$", raw_text, _re.IGNORECASE)
        if (keeper_status or keeper_mark or keeper_skip) and kernel.viewer_person() == "owner":
            # Kiaan's keeper (duty #1, 2026-09-03): the schedule Kyraan is
            # responsible for. Status is a read; recording a dose or a
            # skip is a health note about the child — confirm-gated.
            from kyraan.triggers import kiaan_keeper as _keeper
            _skip_extraction.set(True)
            if keeper_status:
                _history_redaction.set("[showed Kiaan's vaccination status]")
                return _keeper.status_text()
            m = keeper_mark or keeper_skip
            words = m.group(1).strip()
            skipped = bool(keeper_skip)
            when = None
            if keeper_mark and "yesterday" in raw_text.lower():
                from datetime import timedelta as _td
                when = (local_now() - _td(days=1)).date()

            async def _record(_a: dict) -> str:
                labels = _keeper.mark_done(words, when=when, skipped=skipped)
                if not labels:
                    return (f"I couldn't match \"{words}\" to a dose that's still open — "
                            "say \"kiaan status\" to see the names I use.")
                verb = "Noted as skipped" if skipped else "Recorded"
                return f"{verb}: {'; '.join(labels)}. Say \"kiaan status\" for what's next."

            return await _gated(
                chat_id, SkillCall("kiaan.record", {"words": words, "skipped": skipped}), _record,
                describe=(f"About to note that Kiaan {'is SKIPPING' if skipped else 'got'} "
                          f"\"{words}\"" + (f" on {when.strftime('%d %b')}" if when else "")
                          + " (his vaccination record)"))
        open_q = _re.match(
            r"^\s*(?:what'?s\s+(?:still\s+)?open|what\s+needs\s+a\s+reply|anything\s+open|"
            r"open\s+items?|day\s+status|what\s+did\s+i\s+miss)\s*[?!.]*\s*$",
            raw_text, _re.IGNORECASE)
        if open_q and kernel.viewer_person() == "owner":
            from kyraan.triggers import chief_of_staff as _cos_r
            _skip_extraction.set(True)
            _history_redaction.set("[listed what is still open today]")
            return await _cos_r.status_text(chat_id)
        house_q = _re.match(
            r"^\s*(?:house|home)\s+(?:status|report|check)\s*[?!.]*\s*$"
            r"|^\s*how(?:'s|\s+is)\s+the\s+house\s*[?!.]*\s*$",
            raw_text, _re.IGNORECASE)
        if house_q and kernel.viewer_person() == "owner":
            from kyraan.triggers import house_steward as _steward_r
            _skip_extraction.set(True)
            return await _steward_r.status_text()
        vol_m = _re.match(
            r"^\s*(?:(?:can\s+you\s+|please\s+)?(?:set|adjust|make|put|up|turn|change)\s+)?"
            r"(?:the\s+)?(?:echo(?:\s+dot)?\s+)?vol(?:ume|um|\.)?\s*(?:to\s+|at\s+|=\s*)?(\d{1,3})\s*%?\s*"
            r"(?:on\s+(?:the\s+)?echo)?\s*[?!.]*\s*$", raw_text, _re.IGNORECASE)
        if vol_m and kernel.viewer_person() == "owner":
            # "volum 4", "up the volume 7", "adjust echo dot volume to 7"
            # (live 2026-09-02: five deflections asking "4% or level 4/10?").
            # The scale rule already lives in the executor (≤10 is Alexa's
            # scale); the ask never needs a question.
            from kyraan.agents import loop_tools as _lt_v
            _skip_extraction.set(True)
            try:
                out = await _lt_v._speaker_volume(chat_id, {"percent": int(vol_m.group(1))}, raw_text)
            except kernel.ConfirmationRequired:
                out = None
            except kernel.ToolFailed as exc:
                return f"Couldn't set the volume: {exc}"
            if out is None:
                # above the auto cap: the executor asked; route through the gate
                from kyraan.control_plane.kernel import SkillCall as _SC

                async def _set_loud(_a: dict) -> str:
                    r = await _lt_v._speaker_volume(chat_id, {"percent": int(vol_m.group(1))}, raw_text)
                    return f"Volume set to {r.get('volume')}% on {r.get('on', 'the Echo')}."
                return await _gated(chat_id, _SC("home.speaker_volume", {"percent": int(vol_m.group(1))}),
                                    _set_loud, describe=f"About to set the Echo volume to {vol_m.group(1)} (loud)")
            if isinstance(out, dict) and out.get("changed") is False:
                return f"Already at {out.get('volume')}%."
            return f"Volume set to {out.get('volume')}% on {out.get('on', 'the Echo')}."
        photos_m = _re.match(
            r"^\s*(?:show|send|display)\s+(?:me\s+)?(?:all\s+|the\s+|some\s+)?"
            r"(?:(?:latest|recent|last)\s+)?([a-z][a-z .'’-]{1,30}?)(?:'s|’s|s')\s+(?:photos?|pictures?|pics?|images?|moments?)"
            r"\s*[?!.]*\s*$", raw_text, _re.IGNORECASE)
        if photos_m and kernel.viewer_person() == "owner":
            # "show kiaan's photos" (live 2026-09-02: "Which Kiaan photo?"
            # with a list — asking is the failure; sending the latest
            # three is the answer).
            from kyraan.store import persons as _persons_p
            from kyraan.store import documents as _docs_p
            pid = _persons_p.resolve(photos_m.group(1).strip())
            if pid:
                _skip_extraction.set(True)
                _history_redaction.set(f"[sent {photos_m.group(1).strip()}'s latest photos]")
                import asyncio as _aio_p
                rows = await _aio_p.to_thread(_docs_p.list_documents, chat_id, 3, pid, "", "moment")
                rows = rows or await _aio_p.to_thread(_docs_p.list_documents, chat_id, 3, pid, "", "photo")
                if not rows:
                    return f"No photos of {photos_m.group(1).strip()} saved yet — send one and I'll keep it."
                from kyraan.channels import file_send as _fs
                sent = 0
                for r in rows:
                    stored = await _aio_p.to_thread(_docs_p.original_file, chat_id, r["id"])
                    if stored and _fs.available():
                        await _fs.send_stored(chat_id, stored[0], stored[1], caption=r["caption"])
                        sent += 1
                if sent:
                    return f"Sent the latest {sent} of {photos_m.group(1).strip()}'s photos ({len(rows)} listed)."
                return ("I have " + "; ".join(f'"{r["caption"]}" ({r["date"]})' for r in rows)
                        + " — the originals aren't stored for these; re-send one and I'll keep the file.")
        where_q = _re.match(
            r"^\s*(?:where\s+(?:am\s+i|i\s+am)(?:\s+now|\s+right\s+now)?|(?:what'?s\s+|check\s+|show\s+)?my\s+(?:current\s+)?location"
            r"|(?:can\s+you\s+)?check\s+(?:my\s+location\s+)?again|did\s+you\s+get\s+(?:my\s+)?(?:live\s+)?location)\s*[?!.]*\s*$",
            raw_text, _re.IGNORECASE)
        place_m = _re.match(
            r"^\s*(?:remember|save|mark)\s+(?:this\s+)?(?:place|location|spot|here)\s+as\s+(?:the\s+|my\s+)?([a-z][a-z .'-]{1,40}?)\s*[.!]*\s*$",
            raw_text, _re.IGNORECASE)
        forget_place_m = _re.match(r"^\s*forget\s+(?:the\s+)?place\s+(.{2,40}?)\s*[.!]*\s*$", raw_text, _re.IGNORECASE)
        air_rule = _re.match(
            r"^\s*(?:when(?:ever)?\s+)?(?:the\s+)?pm\s*2\.?5\s+(?:is\s+|goes\s+)?(?:above|over|more\s+than|crosses)\s+(\d+)\b.*?\b(turbo|auto|sleep|medium)\b"
            r".*?\b(?:below|under|less\s+than|drops)\b.*?\b(turbo|auto|sleep|medium)\b",
            raw_text, _re.IGNORECASE | _re.DOTALL)
        if air_rule and kernel.viewer_person() == "owner":
            # Live 2026-09-04: "when pm2.5 is above 90 we should switch to
            # turbo mode and it's below we should switch to auto" was
            # refused twice ("rules only notify"). Two acting rules, one
            # confirmation.
            from kyraan.control_plane.kernel import SkillCall as _SC
            from kyraan.triggers import event_rules as _er
            thr, hi_mode, lo_mode = air_rule.group(1), air_rule.group(2).lower(), air_rule.group(3).lower()
            _skip_extraction.set(True)

            async def _make_air_rules(_a: dict) -> str:
                for r in _er.list_active(chat_id):
                    if r.entity == "sensor.air_purifier_pm2_5" and r.action:
                        _er.cancel(chat_id, r.id)
                a = _er.create(chat_id, f"PM2.5 above {thr}: purifier {hi_mode}", "sensor.air_purifier_pm2_5",
                               "above", thr, message=f"PM2.5 is above {thr}", cooldown_minutes=15,
                               action={"tool": "home.purifier", "args": {"mode": hi_mode}})
                b = _er.create(chat_id, f"PM2.5 below {thr}: purifier {lo_mode}", "sensor.air_purifier_pm2_5",
                               "below", thr, message=f"PM2.5 is back below {thr}", cooldown_minutes=15,
                               action={"tool": "home.purifier", "args": {"mode": lo_mode}})
                applied = ""
                try:  # the current reading decides the mode right now, not in 15 minutes
                    if await _er.tick(send=None):
                        applied = " Applied now for the current reading."
                except Exception as exc:
                    log_event("air_rule_apply_failed", error=str(exc)[:80])
                return (f"Done. Above {thr} the purifier goes to {hi_mode}, below {thr} back to {lo_mode} — "
                        f"checked every 15 minutes, and I'll tell you each time it switches.{applied} "
                        f"(\"cancel rule {a.id[:4]}\" or \"{b.id[:4]}\" undoes it.)")
            return await _gated(
                chat_id, _SC("home.purifier", {"mode": hi_mode}), _make_air_rules,
                describe=f"Set two standing rules: PM2.5 above {thr} → purifier {hi_mode}; "
                         f"below {thr} → purifier {lo_mode}. I'd switch it myself and tell you each time")
        phone_q = _re.match(
            r"^\s*(?:(?:what\s+(?:can\s+)?you\s+(?:can\s+)?tell\s+me\s+)?about\s+(?:this|my)\s+phone|(?:my\s+)?phone\s+(?:status|battery|info)"
            r"|(?:how\s+much\s+)?(?:battery|charge)\s+(?:on|in|of)\s+my\s+phone|is\s+my\s+phone\s+charging)\s*[?!.]*\s*$",
            raw_text, _re.IGNORECASE)
        if phone_q and kernel.viewer_person() == "owner":
            from kyraan.triggers import whereabouts as _wh
            _skip_extraction.set(True)
            _history_redaction.set("[reported the owner's phone status]")
            return _wh.phone_text()
        if (where_q or place_m or forget_place_m) and kernel.viewer_person() == "owner":
            # Whereabouts (2026-09-04): the last shared fix, and the
            # owner's own named places for arrival notes.
            from kyraan.triggers import whereabouts as _wh
            _skip_extraction.set(True)
            if where_q:
                _history_redaction.set("[told the owner where they were last seen]")
                return _wh.where_text()
            if forget_place_m:
                name = forget_place_m.group(1).strip().lower()
                return (f"Forgot the place \"{name}\"." if _wh.forget_place(name)
                        else f"No saved place called \"{name}\". Saved: {', '.join(_wh.places()) or 'none'}.")
            name = place_m.group(1).strip().lower()
            saved = _wh.remember_place(name)
            if not saved:
                return ("I need a recent location first — share a pin in Telegram, then say "
                        f"\"remember this place as {name}\".")
            return (f"Saved \"{name}\" as a place ({saved['lat']:.4f}, {saved['lon']:.4f}). "
                    "When you arrive there with live location on, I'll mention what I hold about it.")
        duties_q = _re.match(r"^\s*(?:duties|duty)\s*(?:status|list|report)?\s*[?!.]*\s*$", raw_text, _re.IGNORECASE)
        if duties_q and kernel.viewer_person() == "owner":
            from kyraan.triggers import duties as _duties
            _skip_extraction.set(True)
            return _duties.status_text()
        if _is_greeting(raw_text) and kernel.viewer_person() == "owner":
            # "hello" got "Hello, Maan. What do you want done?" (live
            # 2026-09-04 21:11) — the persona overshooting from eager into
            # servant. A greeting is answered with the time of day and ONE
            # useful glance, deterministically, no model.
            _skip_extraction.set(True)
            return await _greeting_reply(chat_id)
        meds_q = _re.match(
            r"^\s*(?:what\s+(?:are|r)\s+|list\s+|show\s+(?:me\s+)?|tell\s+me\s+)?(?:all\s+)?"
            r"(my|[a-z][a-z .'-]{1,30}?(?:'s|’s|s'))\s+(?:current\s+|saved\s+)?"
            r"(medications?|medicines?|meds|supplements?|prescriptions?|vitamins?)\s*[?!.]*\s*$",
            raw_text, _re.IGNORECASE)
        if meds_q and kernel.viewer_person() == "owner":
            # Whose medicines (live 2026-09-03, three times: "what are my
            # medications?" listed Kiaan's drops and missed the owner's
            # lozenges). Deterministic: the store knows who each capture
            # is about; the answer is that person's, nobody else's.
            from kyraan.store import documents as _docs_m
            from kyraan.store import persons as _persons_m
            who_raw = meds_q.group(1).strip()
            person = ("owner" if who_raw.lower() == "my"
                      else _persons_m.resolve(_re.sub(r"(?:'s|’s|s')$", "", who_raw)))
            if person:
                items = _docs_m.medications_for(chat_id, person)
                _skip_extraction.set(True)
                label = "your" if person == "owner" else f"{who_raw.rstrip(chr(39)+'’s').strip()}'s"
                if not items:
                    return (f"Nothing saved yet as {label} medicines or supplements — "
                            "send a photo of the strip or bottle and I'll file it.")
                lines = [f'• {i["caption"]} — {i["detail"]} ({i["kind"]}, {i["date"]})'
                         if i["detail"] and i["detail"].lower() != i["caption"].lower()
                         else f'• {i["caption"]} ({i["kind"]}, {i["date"]})' for i in items]
                return f"Saved as {label} medicines and supplements:\n" + "\n".join(lines)
        saved_q = _re.match(
            r"^\s*(?:did|have|had)\s+(?:you|u)\s+(?:save|store|keep|index|link)(?:d|ed)?\s+"
            r"(?:it|that|this|them|(?:the|my|this|that)\s+(?:photo|image|picture|pic|doc|document|file|pdf|screenshot|card))"
            r"\s*[?!.]*\s*$", raw_text, _re.IGNORECASE)
        links_q = _re.match(
            r"^\s*(?:show\s+|my\s+|the\s+|its\s+|any\s+)?(?:links?|connections?|relations?)\s*[?!.]*\s*$"
            r"|^\s*(?:what\s+(?:is|are)\s+(?:it|that|they)\s+(?:linked|connected|related)\s+to)\s*[?!.]*\s*$",
            raw_text, _re.IGNORECASE)
        if (saved_q or links_q) and kernel.viewer_person() == "owner":
            # "Did you save it?" / "links?" after a capture (live
            # 2026-09-03 00:45: a photo sent 15 minutes earlier got
            # "what do you mean by it?" — the window had been wiped).
            # The referent is the latest capture, by the store, never
            # by the model's memory of the conversation. The save
            # question names its object, so a day-old capture answers
            # it; the bare "links?" has no object, so it applies only
            # right after a photo (audit 2026-09-03: after a website
            # chat, "links?" would have described yesterday's photo).
            from kyraan.store import documents as _docs_q
            cap = (_docs_q.latest_capture(chat_id) if saved_q
                   else _docs_q.latest_capture(chat_id, max_age_h=1)
                   if _photo_just_sent(chat_id) else None)
            if cap is not None:
                _skip_extraction.set(True)
                # the owner's screen sees every link, including to local-
                # only notes; cloud-bound history keeps a placeholder
                _history_redaction.set("[confirmed the latest capture is saved and listed its links]")
                return _docs_q.describe_capture(cap)
        photo_mine = _re.match(
            r"^\s*(?:this|that|it|here|these|those)(?:\s+(?:is|are)|'s|’s)\s+"
            r"(my\s+[a-z][a-z'-]*(?:\s+[a-z][a-z'-]*){0,2})\s*[.!]?\s*$",
            raw_text, _re.IGNORECASE)
        if (photo_mine and kernel.viewer_person() == "owner"
                and not _re.search(
                    r"\b(?:fault|point|plan|idea|opinion|question|answer|turn|time|day|"
                    r"job|work|mistake|problem|choice|decision|guess|number|first|last|new)\b",
                    photo_mine.group(1), _re.IGNORECASE)
                and _photo_just_sent(chat_id)):
            # After-photo OWNER claim (live 2026-09-03: "this is my
            # medicine" got "Got it" twice and nothing stored changed —
            # the moment stayed nobody's, untitled, uncategorised, so
            # "what are my medications?" had to guess). Deterministic:
            # the just-stored capture becomes the owner's, named by the
            # phrase, categorised by its words. No recent capture: the
            # loop handles the statement as before.
            from kyraan.store import documents as _docs0
            claimed = _docs0.claim_latest_moment(chat_id, photo_mine.group(1))
            if claimed is not None:
                caption, ents = claimed
                _skip_extraction.set(True)
                tag = next((e for e in ents if e.startswith("#")), "")
                return (f'Noted — that photo is yours now: "{caption}"'
                        + (f", filed under {tag}" if tag else "") + ".")
        add_person = _re.match(
            r"^\s*(?:please\s+)?(?:create|add|register|make)\s+(?:a\s+|an\s+)?(?:new\s+)?"
            r"(?:person|contact|people\s+entry)\s*(?:from\s+(?:the\s+|his\s+|her\s+)?face\s*)?"
            r"(?:for|named|called|of|:)?\s+([A-Za-z][A-Za-z .'-]{1,40}?)\s*[.!]?\s*$",
            raw_text, _re.IGNORECASE)
        if add_person and kernel.viewer_person() == "owner":
            # "create a person from face for akansha" (live 2026-09-03:
            # answered "I can't" while persons.add existed). Deterministic
            # and confirm-gated like every registry write; an enrolled
            # face with that name is linked by the executor.
            from kyraan.agents import loop_tools as _lt
            from kyraan.store import persons as _persons_a
            wanted = add_person.group(1).strip()
            if _persons_a.resolve(wanted):
                _skip_extraction.set(True)
                return f"{wanted} is already in the person registry."
            try:
                from kyraan.agents import faces as _faces_a
                face = next((f for f in _faces_a.enrolled_names()
                             if f.lower() == wanted.lower() or f.lower().startswith(wanted.lower() + " (")), "")
            except Exception:
                face = ""

            async def _add_person(_a: dict) -> str:
                out = await _lt._persons_add(chat_id, {"name": wanted}, raw_text)
                if not out.get("added"):
                    return out.get("note", "Nothing changed.")
                linked = out.get("face_linked") or []
                return (f"Added {wanted} to the person registry"
                        + (f' and linked the enrolled face "{linked[0]}" to them' if linked else "")
                        + ". Photos and documents can link to them now; no access was granted.")

            _skip_extraction.set(True)
            return await _gated(
                chat_id, SkillCall("persons.add", {"name": wanted}), _add_person,
                describe=(f"About to add {wanted} to the person registry"
                          + (f' and link the enrolled face "{face}" to them' if face else "")
                          + " (no access of any kind is granted)"))
        photo_person = _re.match(
            r"^\s*(?:she|he|that|this)\s+is\s+([a-z][\w .-]{1,40}?)\s*[.!]?\s*$",
            raw_text, _re.IGNORECASE)
        if photo_person and _photo_just_sent(chat_id):
            # After-photo person correction (live 2026-09-02: "she is
            # kiaan's mom" was acknowledged and NOTHING stored changed;
            # worse, the face matcher had named the adult "kiaan").
            # Deterministic when the words resolve in the registry: link
            # the person to the just-stored moment; a contradicting
            # face match is flagged for the owner. Unresolvable words
            # ("kiaan's mom") fall through to the loop as before.
            from kyraan.store import documents as _docs
            from kyraan.store import persons as _persons
            pid = _persons.resolve(photo_person.group(1).strip())
            if pid and pid != "owner":
                linked = _docs.link_person_to_latest_moment(chat_id, pid)
                if linked is not None:
                    caption, prior = linked
                    _skip_extraction.set(True)
                    reply = (f'Linked {photo_person.group(1).strip()} to '
                             f'that photo ("{caption}").')
                    if prior and pid not in prior:
                        other = [p for p in prior if p != pid]
                        if other:
                            log_event("face_match_suspect",
                                      matched=other, corrected_to=pid)
                            reply += (
                                f"\n\n⚠️ My face match had named "
                                f"{', '.join(other)} here — if that keeps "
                                "happening, re-enroll their face from "
                                "3-4 clear solo photos (\"remember this "
                                "face as …\").")
                    return reply
        if (_re.match(r"^\s*(?:show|explain)\s+(?:the\s+)?last\s+turn\s*[?!.]?\s*$"
                      r"|^\s*why\s+did\s+(?:that|the last)\s+turn\s+"
                      r"(?:end|fail|do that)\s*[?!.]?\s*$",
                      raw_text, _re.IGNORECASE)
                and kernel.viewer_person() == "owner"):
            # Turn introspection (2026-09-01): the drill-down that used
            # to need log grepping, deterministic and owner-only. Reads
            # the PREVIOUS turn's trace + events; never a model call.
            _skip_extraction.set(True)
            return _describe_last_turn(chat_id)
        vault_m = _re.match(
            r"^\s*(re-?index|index|force\s+index)\s+(?:my\s+|the\s+)?(?:vault|notes?|obsidian(?:\s+notes?)?)\s*[?!.]?\s*$",
            raw_text, _re.IGNORECASE)
        if vault_m and kernel.viewer_person() == "owner":
            # Deterministic, owner-only: a vault sync on demand (nightly
            # otherwise). "index" is change-aware — unchanged files are
            # skipped by hash; "reindex"/"force index" re-applies the
            # indexer's CURRENT rules to every file (live 2026-09-02: the
            # owner re-indexed after a linking fix and nothing re-linked,
            # because the notes themselves hadn't changed). Read-only
            # against the vault by construction.
            _skip_extraction.set(True)
            import asyncio as _aio_v

            from kyraan.store import notes as _notes
            force = vault_m.group(1).lower() != "index"
            counts = await _aio_v.to_thread(_notes.sync, chat_id, None, force)
            if "error" in counts:
                return ("Vault indexing isn't set up — put your vault path "
                        "in KYRAAN_VAULT_ROOT (.env) and restart me.")
            return ((f"Vault re-indexed (all rules re-applied): " if force
                     else "Vault indexed: ")
                    + f"{counts['indexed']} {'notes' if force else 'new/changed notes'}, "
                    f"{counts['unchanged']} unchanged, {counts['removed']} "
                    f"removed, {counts['skipped']} skipped. Ask me anything "
                    "from your notes.")
        if (_re.match(r"^\s*list\s+learned\s+rules\s*[?!.]?\s*$",
                      raw_text, _re.IGNORECASE)
                and kernel.viewer_person() == "owner"):
            from kyraan.memory import lessons as _lessons
            _skip_extraction.set(True)
            rules = _lessons.active_rules()
            if not rules:
                return ("No learned rules yet — when you correct me the "
                        "same way a few times, I'll propose one for your "
                        "review.")
            return "Learned rules (owner-approved):\n" + "\n".join(
                f"{i+1}. {r['rule']}  [{r['id'][:6]}]"
                for i, r in enumerate(rules)) + \
                '\n\nSay "retire learned rule <words or id>" to drop one.'
        retire_m = _re.match(r"^\s*retire\s+learned\s+rule\s+(.+?)\s*$",
                             raw_text, _re.IGNORECASE)
        if retire_m and kernel.viewer_person() == "owner":
            from kyraan.memory import lessons as _lessons
            _skip_extraction.set(True)
            try:
                gone = _lessons.retire(retire_m.group(1))
            except ValueError as exc:
                return str(exc)
            return f"Retired: \"{gone['rule']}\" — it no longer shapes my replies."
        if _re.match(r"^\s*healt?h?\s+(?:report|check|status)\s*[?!.]?\s*$"
                     r"|^\s*health\s*[?!.]?\s*$",
                     raw_text, _re.IGNORECASE) and kernel.viewer_person() == "owner":
            # typo-tolerant ("healt report" seen live 13:52 — the loop
            # improvised a generic question instead)
            # Deterministic, owner-only: the doctor's full report in chat.
            _skip_extraction.set(True)
            import asyncio as _aio

            from kyraan.control_plane import health
            verdict, text = await _aio.to_thread(health.report)
            _history_redaction.set("[showed the health report]")
            return f"🩺 {verdict}\n{text}"

        if _CONSOLIDATE_RE.match(raw_text):
            # Deterministic, like review: the nightly scan invites this
            # phrase; the apply must never depend on model routing.
            _skip_extraction.set(True)
            return await _consolidate_memory(chat_id)

        undo_match = _UNDO_RE.match(raw_text)
        if undo_match:
            # Deterministic, like forget-face: reversing an action must
            # never depend on a model's routing choice (P3.1c).
            _skip_extraction.set(True)
            return await _undo_command(
                chat_id, _UNDO_TARGETS.get((undo_match.group(1) or "").lower()))

        if _is_review_request(raw_text):
            # Deterministic: the review flow OWNS these phrases. Routed
            # through the agent loop, "review memory" listed the queue via
            # a read-only tool and the owner's "approve all" then had no
            # session to act on (live) — the approval path must never
            # depend on a model's routing choice.
            return await _review_memory(chat_id, raw_text)

        # ONE brain, two tiers (arch §1, delivered by P3.7b): the same
        # agent loop runs on the frontier, then on the local cheap tier
        # behind the same doctrine — degraded mode is a smaller model,
        # not a different system. Both tiers failing is an OUTAGE and is
        # reported as one; the legacy classifier was retired 2026-08-27
        # after two consecutive all-green degraded eval runs (P3.7a).
        # "Did you mean ...?" (owner 2026-09-03): a short message that
        # matched no rail but sits close to an exact command is offered
        # the command instead of a model round that asks what they meant
        # ("index memory" / "index note" -> "reindex vault"). Only when
        # the match is strong and the phrase has no free slot to fill.
        is_cap_q = _commands.is_capability_question(raw_text)
        # a plain question ("any reminders?") is for the model — eval
        # reminder.list caught the over-reach 2026-09-03
        plain_question = raw_text.strip().endswith("?") and not is_cap_q
        if kernel.viewer_person() == "owner" and not _secret_turn.get() and not plain_question and (
                len(raw_text.split()) <= 7 or (is_cap_q and len(raw_text.split()) <= 16)):
            found = _commands.suggest(raw_text, min_score=0.6)
            if found and "<" not in found[0][0] and found[0][0].lower() != raw_text.strip().lower():
                phrase, what, _score = found[0]
                _pending_suggestion[chat_id] = (phrase, time.monotonic())
                _skip_extraction.set(True)
                log_event("command_suggested", chat_id=chat_id, phrase=phrase, text=raw_text[:80])
                if is_cap_q:
                    # "is there anything that can index obsidian notes?" —
                    # yes, and here is the phrase (owner 2026-09-03)
                    return (f'Yes — say "{phrase}": {what}. Reply "yes" to run it now, '
                            'or "help" for all the exact commands.')
                return (f'Did you mean "{phrase}" — {what}? Reply "yes" to run it, '
                        'or "help" for all the exact commands.')
        from kyraan.agents import agent_loop
        secret = _secret_turn.get()
        # frontier -> standby (the previous frontier model, cloud) -> cheap
        # (local): a capped or failing mini falls to nano before the loop
        # degrades to qwen3 (2026-09-04, mini trial)
        if secret:
            # a private turn never runs the tool loop (see secrets.local_reply)
            from kyraan.agents import secrets as _secrets_lr
            text = await _secrets_lr.local_reply(chat_id, raw_text)
            if text:
                return text
            log_event("secret_turn_unanswered", chat_id=chat_id)
            return ("I couldn't get an answer from the local model just now, "
                    "and I won't send a private message to the cloud. Nothing "
                    "was stored — try again in a moment, or say \"private mode "
                    "off\" if this doesn't need to stay private.")
        if kernel.viewer_person() == "owner" and _re.search(
                r"\b(explain|summar|what(?:'s| is| does)|tell me|read|open|details?|amount|total|how much|when|who|where)\b",
                raw_text, _re.IGNORECASE):
            # A question that names a PRIVATE document is the local
            # model's, with the document — never the cloud's (2026-09-04).
            try:
                import asyncio as _aio
                from kyraan.store import documents as _docs
                doc = await _aio.to_thread(_docs.local_only_match, chat_id, raw_text)
            except Exception:
                doc = None
            if doc:
                from kyraan.agents import secrets as _secrets_doc
                _skip_extraction.set(True)
                _history_redaction.set("[answered about a private document]")
                _answered_by.set("local")
                text = await _secrets_doc.local_document_reply(chat_id, raw_text, doc)
                log_event("private_document_answered", ok=bool(text), doc=doc["caption"][:40])
                return (f"🔒 {text}" if text else
                        f"\"{doc['caption']}\" is a private document, so I only read it on the local "
                        "model — and it gave me nothing just now. Try again in a moment.")
        for tier in tier_chain():
            if tier == "cheap":
                # P3.7a: the local model now holds BOTH the loop and
                # extraction — the extraction cutoff widens for this
                # turn or contention silently eats every "Noted for
                # review" (9x in one degraded eval run).
                _degraded_turn.set(True)
            try:
                _answered_by.set("local" if tier == "cheap" else "cloud")
                return await agent_loop.run(chat_id, raw_text, tier=tier)
            except KillSwitchEngaged:
                raise
            except agent_loop.AgentUnavailable as exc:
                log_event("agent_tier_fallback", tier=tier, error=str(exc)[:200])
            except Exception as exc:
                log_event("agent_loop_error", tier=tier, error=str(exc),
                          error_type=type(exc).__name__)
        log_event("agent_all_tiers_failed")
        return ("Both my reasoning models are unreachable right now, so I "
                "couldn't act on that — nothing was done. Reminders and "
                "scheduled tasks still fire on their own; try me again in "
                "a few minutes.")
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
        log_event("handle_message_error", user_text=raw_text, error=str(exc), error_type=type(exc).__name__)
        return "Something went wrong handling that — try again, or rephrase."


# --- semantic memory consolidation (chat surface) -------------------------

async def _consolidate_memory(chat_id: int) -> str:
    """Scan (frontier model proposes), then a confirm ask that NAMES
    every group; the owner's yes applies exactly the stashed proposals —
    supersession, never deletion."""
    import asyncio as _aio

    from kyraan.memory import consolidate
    try:
        proposals = await _aio.to_thread(consolidate.scan)
    except Exception as exc:
        log_event("consolidation_scan_failed", error=str(exc)[:150])
        return ("The dedup scan needs the frontier model and it isn't "
                "reachable right now — try again later.")
    if not proposals:
        return "Memory is clean — no duplicate facts found."
    lines = []
    for n, p in enumerate(proposals, 1):
        dups = "; ".join(f'"{c}"' for _, c in p["duplicates"])
        lines.append(f'{n}. keep "{p["keep_content"]}" — supersede {dups}')
    describe = ("Consolidate memory (duplicates become history, nothing "
                "is deleted):\n" + "\n".join(lines))

    async def _apply(_a: dict) -> str:
        superseded = []
        for p in proposals:  # the exact stashed proposals, nothing re-scanned
            superseded += consolidate.apply(
                p["keep"], [d for d, _ in p["duplicates"]])
        _skip_extraction.set(True)
        if not superseded:
            return "Nothing needed doing — those facts were already consolidated."
        kept = "\n".join(f'• {p["keep_content"]}' for p in proposals)
        return (f"Done — {len(superseded)} duplicate fact(s) are now "
                f"history. Kept:\n{kept}")

    return await _gated(chat_id, SkillCall("memory.consolidate",
                                           {"groups": len(proposals)}),
                        _apply, describe=describe)


async def _forget_document(chat_id: int, wanted: str) -> str:
    """'forget the document <words>' — hybrid-match the capture, confirm
    naming exactly what dies, hard-delete on yes (chunks cascade)."""
    import asyncio as _aio

    from kyraan.store import documents
    try:
        hits = await _aio.to_thread(documents.search, chat_id, wanted, 3)
    except Exception as exc:
        return ("Document memory isn't reachable right now "
                f"({str(exc)[:80]}) — nothing was deleted.")
    if not hits:
        return f'No saved document matches "{wanted}" — nothing to forget.'
    named = "; ".join(f'"{h["caption"]}" ({h["date"]})' for h in hits)

    async def _delete(_a: dict) -> str:
        gone = await _aio.to_thread(
            documents.delete_documents, chat_id,
            [h["doc_id"] for h in hits])
        if not gone:
            return "Those documents were already gone."
        return ("Deleted from document memory: "
                + "; ".join(f'"{c}"' for c in gone) + ".")

    return await _gated(
        chat_id, SkillCall("documents.forget", {"query": wanted}), _delete,
        describe=f"About to DELETE {len(hits)} saved document(s): {named}")


# --- P3.1c: the undo command ----------------------------------------------

def _age_note(action) -> str:
    """A stale target must be visible in the ask: a bare "undo" reached
    a two-hour-old AC switch and surprised the owner (2026-08-27).
    Anything older than ~10 minutes names its time."""
    try:
        from kyraan.control_plane.dnd import local_now
        done = action.done_at
        age_s = (local_now() - done.astimezone(local_now().tzinfo)
                 ).total_seconds()
        if age_s > 600:
            return f" (from {done.astimezone(local_now().tzinfo).strftime('%-I:%M %p')})"
    except Exception:
        pass
    return ""


def _describe_undo(action) -> str:
    """The confirm ask names the INVERSE concretely — the owner approves
    a specific reversal, not a vague 'undo'."""
    a, ua = action.args, action.undo_args or {}
    if action.tool == "calendar.create_event":
        return f'Undo: delete the event "{ua.get("title") or a.get("title", "")}" I just created'
    if action.tool == "reminders.create":
        return f'Undo: cancel the reminder "{a.get("text", "")}" I just set'
    if action.tool in ("reminders.snooze", "reminders.reschedule"):
        return (f'Undo: move that reminder back to its earlier time'
                if action.undo_tool == "reminders.reschedule"
                else "Undo: cancel the snoozed reminder I just added")
    if action.tool == "calendar.reschedule":
        return (f'Undo: move "{ua.get("title") or a.get("event_id")}" back '
                "to its previous time")
    if action.tool == "tasks.schedule":
        return f'Undo: cancel the scheduled task "{str(a.get("instruction", ""))[:60]}"'
    if action.tool == "faces.remember":
        return f'Undo: delete the face I just saved as "{ua.get("name", "")}"'
    if action.tool in ("home.turn_on", "home.turn_off"):
        back = "on" if action.undo_tool == "home.turn_on" else "off"
        return (f'Undo: switch {ua.get("entity", "")} back {back}'
                + _age_note(action))
    if action.tool == "documents.rename":
        return (f'Undo: rename that document back to '
                f'"{ua.get("new_name", "")}"')
    if action.tool == "email.draft":
        return (f'Undo: delete the Gmail draft "{a.get("subject") or ""}" '
                "I just saved")
    if action.tool == "email.mark_read":
        return "Undo: mark that email unread again" + _age_note(action)
    if action.tool == "email.archive":
        return ("Undo: restore that email to the inbox"
                + _age_note(action))
    if action.tool == "calendar.delete_event":
        return (f'Undo: re-create the event '
                f'"{ua.get("title") or "(restored event)"}" I deleted'
                + _age_note(action))
    if action.tool == "reminders.cancel":
        return (f'Undo: re-create the reminder "{ua.get("text", "")}" '
                "I cancelled" + _age_note(action))
    if action.tool == "tasks.cancel":
        return (f'Undo: re-schedule the task '
                f'"{str(ua.get("instruction", ""))[:60]}" I cancelled'
                + _age_note(action))
    if action.tool == "rules.cancel":
        return "Undo: switch that cancelled watch rule back on"
    if action.tool == "memory.forget":
        return ("Undo: RESTORE to memory what I forgot "
                f'(matching "{a.get("fact", "")}")' + _age_note(action))
    return f"Undo the last action ({action.tool})"


_UNDO_TARGET_WORD = {"reminders.": "reminder", "tasks.": "task",
                     "calendar.": "event", "home.": "switch",
                     "faces.": "face"}


async def _undo_command(chat_id: int, tool_prefix: str | None) -> str:
    import asyncio as _aio

    from kyraan.store import actions as _actions
    try:
        action = await _aio.to_thread(
            (lambda: _actions.last_action_of(chat_id, tool_prefix))
            if tool_prefix else (lambda: _actions.last_action(chat_id)))
    except Exception as exc:
        log_event("undo_store_unreachable", chat_id=chat_id, error=str(exc)[:200])
        return ("I can't reach the action log right now, so undo isn't "
                "available — the action itself is unaffected.")
    if action is None:
        what = (f"recent {_UNDO_TARGET_WORD.get(tool_prefix, 'such')} action"
                if tool_prefix else "recent action")
        return f"Nothing to undo — I have no {what} on record."
    if not action.undoable:
        # Head honesty (audit P1): never silently reach past an
        # irreversible newest action — name it, then name what a
        # TARGETED undo could still reach.
        reply = f"Your last action ({action.tool}) can't be undone."
        try:
            reachable = await _aio.to_thread(_actions.last_undoable, chat_id)
        except Exception:
            reachable = None
        if reachable and not tool_prefix:
            word = next((w for p, w in _UNDO_TARGET_WORD.items()
                         if reachable.tool.startswith(p)), None)
            if word:
                reply += (f' Still reversible: say "undo the {word}" to '
                          f"{_describe_undo(reachable)[6:].strip()}.")
        return reply

    async def _run_undo(_a: dict) -> str:
        ut, ua = action.undo_tool, dict(action.undo_args or {})
        try:
            if ut in ("calendar.delete_event", "calendar.update_event",
                      "calendar.create_event",
                      "home.turn_on", "home.turn_off"):
                await kernel.run_tool(kernel.ToolCall(ut, ua))
            elif ut == "reminders.recreate":
                scheduler.create_reminder(
                    chat_id, ua["text"], ua["when_iso"],
                    repeat=ua.get("repeat") or "",
                    interval_minutes=int(ua.get("interval_minutes") or 0),
                    window_start=ua.get("window_start") or "",
                    window_end=ua.get("window_end") or "")
            elif ut == "tasks.recreate":
                from kyraan.triggers import agent_tasks as _tasks
                _tasks.create(chat_id, ua["instruction"], ua["when_iso"],
                              repeat=ua.get("repeat") or "")
            elif ut == "rules.reactivate":
                from kyraan.triggers import event_rules
                try:
                    event_rules.reactivate(chat_id, ua["rule_id"])
                except ValueError as exc:
                    raise kernel.ToolFailed(str(exc))
            elif ut == "memory.unforget":
                from kyraan.memory import engine as _engine
                if not await _aio.to_thread(_engine.unforget,
                                            list(ua.get("entry_ids") or [])):
                    raise kernel.ToolFailed(
                        "those facts are no longer restorable")
            elif ut == "reminders.cancel":
                from kyraan.agents import loop_tools
                await loop_tools._reminders_cancel_gated(
                    chat_id, {"reminder_id": ua["reminder_id"]})
            elif ut == "reminders.reschedule":
                scheduler.reschedule_reminder(
                    chat_id, ua["reminder_id"], ua["when_iso"])
            elif ut == "rules.cancel":
                from kyraan.triggers import event_rules
                event_rules.cancel(chat_id, ua["rule_id"])
            elif ut == "email.draft_delete":
                from kyraan.tools import gmail as _gmail
                if not await _aio.to_thread(
                        _gmail._delete_draft, str(ua.get("draft_id", ""))):
                    raise kernel.ToolFailed("that draft is already gone")
            elif ut in ("email.mark_unread", "email.unarchive"):
                # The undo inverse gets the same read-after-write check
                # as its forward twin (verification completeness,
                # 2026-08-31): the label must actually be back.
                from kyraan.tools import gmail as _gmail
                label = "UNREAD" if ut == "email.mark_unread" else "INBOX"
                await _aio.to_thread(_gmail.set_labels,
                                     ua["message_id"], [label], [])
                try:
                    labels = await _aio.to_thread(_gmail.message_labels,
                                                  ua["message_id"])
                except Exception:
                    labels = None
                if labels is not None and label not in labels:
                    raise kernel.ToolFailed(
                        "the undo did not stick on re-read — check the "
                        "email in Gmail")
            elif ut == "persons.set_tools":
                from kyraan.store import persons as _persons
                pid = _persons.resolve(ua["name"]) or ua["name"]
                current = set(_persons.extra_tools(pid))
                _persons.set_extra_tools(
                    pid, sorted((current | set(ua.get("grant") or []))
                                - set(ua.get("revoke") or [])))
            elif ut == "persons.set_access":
                from kyraan.store import persons as _persons
                prow = next((p for p in _persons.list_persons()
                             if p[0] == ua["name"]), None)
                if prow is None:
                    raise kernel.ToolFailed("that person is no longer registered")
                _persons.enroll(ua["name"], prow[1], ua["stage"], prow[3])
            elif ut == "documents.rename":
                from kyraan.store import documents as _documents
                hits = await _aio.to_thread(
                    _documents.search, chat_id, ua["query"])
                if not hits or await _aio.to_thread(
                        _documents.rename_document, chat_id,
                        hits[0]["doc_id"], ua["new_name"]) is None:
                    raise kernel.ToolFailed("that document is no longer there")
            elif ut == "tasks.cancel":
                from kyraan.agents import loop_tools
                await loop_tools._task_cancel(chat_id, ua, "")
            elif ut == "faces.forget":
                from kyraan.agents import faces as _faces
                if not _faces.forget(str(ua.get("name", ""))):
                    raise kernel.ToolFailed("that face is no longer stored")
            else:
                raise kernel.ToolFailed(f"no undo executor for {ut}")
        except kernel.ToolFailed as exc:
            # The original action stays on the log — it was NOT undone.
            return f"Couldn't undo that: {exc}"
        await _aio.to_thread(_actions.mark_undone, action.id)
        log_event("action_undone", chat_id=chat_id, tool=action.tool,
                  undo_tool=ut)
        return f"Done — undone. ({_describe_undo(action)[6:].strip()})"

    return await _gated(chat_id, SkillCall("undo.last", {"tool": action.tool}),
                        _run_undo, describe=_describe_undo(action))


def _load_persisted_confirmation(chat_id: int):
    """P3.4b: a loop-tool ask persisted in Redis (restart survivor).
    Returns (call, handler, stashed_at) shaped exactly like the
    in-process stash, or None. Redis TTL is the expiry — a record that
    still exists is within the confirmation window."""
    from kyraan.store import redis_kv
    record = redis_kv.get_json(redis_kv.key("confirm", chat_id))
    if not record:
        return None
    try:
        from kyraan.agents import agent_loop
        call = SkillCall(record["skill"], dict(record.get("skill_args") or {}))
        handler = agent_loop.build_confirmed_handler(
            chat_id, record["tool"], dict(record.get("args") or {}),
            str(record.get("raw_text") or ""))
        log_event("confirmation_restored", chat_id=chat_id,
                  skill=record["skill"], tool=record["tool"])
        return (call, handler, time.monotonic())
    except Exception as exc:
        log_event("confirmation_restore_failed", error=str(exc)[:150])
        return None


def current_confirmation_nonce(chat_id: int) -> str:
    """The nonce for this chat's pending ask — in-process first, then
    the Redis survivor (so the ask's Yes/No BUTTONS also work across a
    restart, not just a typed yes)."""
    nonce = _confirmation_nonce.get(chat_id, "")
    if nonce:
        return nonce
    from kyraan.store import redis_kv
    record = redis_kv.get_json(redis_kv.key("confirm", chat_id))
    return str(record.get("nonce", "")) if record else ""


async def _gated(chat_id: int, call: SkillCall, handler, describe: str = "",
                 replay: dict | None = None) -> str:
    """Run a skill through the kernel; if it (or a confirm-gated tool
    inside it) needs approval, stash it and ask — `describe` names the
    concrete action so the user confirms a specific thing, not a vague
    intent.

    `replay` (P3.4b): the loop-tool asks are REBUILDABLE from
    (tool, args, raw_text), so they also persist to Redis with the
    confirmation TTL — the ask now survives a restart, and the owner's
    yes in the NEW process replays the same call byte-identically.
    Closure-bound asks (faces, review, undo, consolidate) stay
    process-local and keep the honest orphan reply."""
    try:
        return str(await kernel.run_skill(call, handler))
    except ConfirmationRequired:
        import uuid as _uuid
        # The message just became a confirm-gated ACTION — it is a
        # command, not a fact, and must not ALSO land in the memory
        # review queue (live: scheduling a task filed "you want to check
        # tomorrow's calendar every evening" as a durable fact).
        _skip_extraction.set(True)
        nonce = _uuid.uuid4().hex[:12]
        _confirmation_nonce[chat_id] = nonce
        _pending_confirmations[chat_id] = (call, handler, time.monotonic())
        from kyraan.store import redis_kv
        if replay is not None:
            redis_kv.set_json(
                redis_kv.key("confirm", chat_id),
                {"skill": call.skill_name, "skill_args": dict(call.args),
                 "nonce": nonce, "describe": describe, **replay},
                ttl_s=int(_CONFIRMATION_TTL_S))
        else:
            # A closure-bound ask REPLACES any persisted one — a stale
            # Redis stash must never outlive the newer in-process ask.
            redis_kv.delete(redis_kv.key("confirm", chat_id))
        what = describe or f"'{call.skill_name}' needs your confirmation first"
        return f"{what} — reply \"yes\" to confirm or \"no\" to cancel."





