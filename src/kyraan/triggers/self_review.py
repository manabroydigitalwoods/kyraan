"""Nightly self-review — Kyraan critiques its own day (harness pack B,
the seed of Phase 4's reflection engine).

Reads today's owner conversation from chat.jsonl — using each entry's
cloud_text twin and the legacy-template redactor, so nothing crosses a
boundary the live conversation didn't — asks the frontier model for an
honest critique, and sends the owner a short report. It proposes; it
never modifies anything.
"""
import json
from datetime import datetime, time

from kyraan.control_plane import config, kernel, logging_setup
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.logging_setup import log_event
from kyraan.model_router import router

_MIN_MESSAGES = 6  # a quiet day yields no report

_SYSTEM = """You are reviewing ONE day of a personal assistant's chat with its
owner. Identify where the assistant did badly: misunderstandings, wrong or
unhelpful answers, missed intent, awkward repetition, broken promises.
Also note one thing it did notably well. Be concrete — quote the moment.
Output a SHORT owner-facing report:
- first line: "Reviewed N exchanges today. X looked wrong."
- then at most 4 bullets (the problems, quoted briefly, plus the one good
  thing). No preamble, no advice essay, no markdown headers."""


def review_time() -> time | None:
    cfg = (config.load().get("self_review") or {})
    if not cfg.get("enabled"):
        return None
    hh, mm = str(cfg.get("time", "21:45")).split(":")
    return time(int(hh), int(mm))


def _todays_transcript(chat_id: int) -> list:
    from kyraan.agents.orchestrator import _legacy_cloud_placeholder

    today = local_now().date()
    tz = local_now().tzinfo
    lines = []
    try:
        raw = logging_setup.CHAT_LOG.read_text(errors="replace").splitlines()
    except OSError:
        return []
    parsed = []
    for line in raw[-3000:]:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    from kyraan.agents.secrets import apply_redactions
    for entry in apply_redactions(parsed):   # a redacted secret never reaches the critic
        if entry.get("chat_id") != chat_id:
            continue
        try:
            when = datetime.fromisoformat(entry["ts"]).astimezone(tz)
        except (KeyError, ValueError):
            continue
        if when.date() != today:
            continue
        role = entry.get("role")
        if role not in ("user", "assistant", "proactive"):
            continue
        text = entry.get("cloud_text") or entry.get("text") or ""
        if role == "assistant" and "cloud_text" not in entry:
            text = _legacy_cloud_placeholder(text) or text
        lines.append((when.strftime("%H:%M"), role, text[:400]))
    return lines


_CRITIC_SYSTEM = """You are auditing the SYSTEM PROMPT of a personal assistant
against one day's operational signals (guard firings, tool failures,
fallbacks, cache/latency stats). Propose at most 3 SPECIFIC prompt edits,
each tied to a signal as evidence — or say the prompt needs no change.
Rules you must respect:
- Every existing rule cites a live failure; never propose deleting one
  without addressing what it prevented.
- The static prefix must stay byte-stable across calls (prompt caching);
  never propose inserting dynamic values into it.
- Proposals ONLY — the owner applies edits by hand, gated by the eval
  suite. Output: short bullets, each "EDIT: <what> — EVIDENCE: <signal>".
No preamble."""

# Event kinds that indicate the prompt/guards were fighting the model —
# the raw material a prompt critique can actually stand on.
_SIGNAL_KINDS = (
    "agent_deflection_corrected", "agent_tier_fallback",
    "agent_fallback_classifier", "web_taint_blocked_tool",
    "tool_loop_detected", "normalized_text_rejected",
    "reminder_intent_demoted", "home_query_demoted",
    "places_google_fallback", "routes_tomtom_fallback",
    "confirmation_replay_regated",
)


def _day_signals() -> str:
    """Deterministic digest of today's events — counts, guard-fire draft
    snippets, tool failures, cache/latency stats. No memory facts, no
    pending proposals: everything here either already crossed the cloud
    boundary today or is a number."""
    from collections import Counter

    today = local_now().date()
    tz = local_now().tzinfo
    counts: Counter = Counter()
    drafts: list = []
    tool_failures: Counter = Counter()
    latencies: list = []
    cache_eligible = cache_missed = 0
    try:
        raw = logging_setup.EVENT_LOG.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for line in raw[-20000:]:
        try:
            e = json.loads(line)
            when = datetime.fromisoformat(e["ts"]).astimezone(tz)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if when.date() != today:
            continue
        kind = e.get("kind", "")
        if kind in _SIGNAL_KINDS:
            counts[kind] += 1
            if kind == "agent_deflection_corrected" and e.get("draft") and len(drafts) < 6:
                drafts.append(str(e["draft"])[:120])
        elif kind == "tool_result" and e.get("ok") is False:
            tool_failures[e.get("tool") or e.get("skill") or "?"] += 1
        elif kind == "model_call":
            latencies.append(int(e.get("latency_ms") or 0))
            if int(e.get("input_tokens") or 0) >= 1024:
                cache_eligible += 1
                if int(e.get("cached_tokens") or 0) == 0:
                    cache_missed += 1
    if not counts and not tool_failures:
        return ""
    lines = ["GUARD/FALLBACK FIRINGS TODAY:"]
    lines += [f"- {k}: {v}x" for k, v in counts.most_common()]
    if drafts:
        lines.append("DEFLECTION DRAFTS THE GUARD CAUGHT (verbatim snippets):")
        lines += [f'- "{d}"' for d in drafts]
    if tool_failures:
        lines.append("TOOL FAILURES:")
        lines += [f"- {k}: {v}x" for k, v in tool_failures.most_common(8)]
    if latencies:
        latencies.sort()
        lines.append(
            f"MODEL CALLS: {len(latencies)}, p50={latencies[len(latencies)//2]}ms, "
            f"p95={latencies[int(len(latencies)*0.95)]}ms; full cache misses "
            f"{cache_missed}/{cache_eligible} eligible")
    return "\n".join(lines)


async def _prompt_critique() -> str:
    """One frontier call over the day's signals + the STATIC prompt
    sections (doctrine, tool menu, capability brief — never the memory
    block or pending facts). Returns '' when there's nothing to critique."""
    if not (config.load().get("self_review") or {}).get("prompt_critic"):
        return ""
    signals = _day_signals()
    if not signals:
        return ""
    from kyraan.agents import agent_loop
    from kyraan.agents.capabilities import capability_brief
    static_prompt = agent_loop._AGENT_SYSTEM.format(
        capabilities=capability_brief(), tools=agent_loop._tools_block())
    response = await router.acall(
        prompt=f"{signals}\n\n=== THE SYSTEM PROMPT UNDER AUDIT ===\n{static_prompt}",
        system=_CRITIC_SYSTEM, tier="frontier", max_tokens=500)
    return response.text.strip()


async def compose(chat_id: int) -> str:
    """The report text, or '' when the day is too quiet to review."""
    transcript = _todays_transcript(chat_id)
    user_messages = sum(1 for _, role, _ in transcript if role == "user")
    if user_messages < _MIN_MESSAGES:
        log_event("self_review_skipped", reason="quiet day", messages=user_messages)
        return ""
    rendered = "\n".join(f"[{ts}] {role}: {text}" for ts, role, text in transcript)
    response = await router.acall(prompt=rendered, system=_SYSTEM,
                                  tier="frontier", max_tokens=600)
    report = response.text.strip()
    if not report:
        return ""
    try:
        critique = await _prompt_critique()
    except Exception as exc:  # the critic must never sink the review
        log_event("prompt_critic_error", error=str(exc)[:200])
        critique = ""
    if critique:
        report += ("\n\n🛠 Prompt critique (proposals only — edits stay "
                   "yours, gated by scripts/eval.py):\n" + critique)
        log_event("prompt_critic_included")
    return f"🔍 Daily self-review\n\n{report}"


async def fire(chat_id: int, send_fn) -> bool:
    if not kernel.can_send_proactively():
        log_event("self_review_skipped", reason="dnd or kill switch")
        return False
    try:
        report = await compose(chat_id)
    except Exception as exc:
        log_event("self_review_error", error=str(exc)[:200])
        return False
    if not report:
        return False
    await send_fn(chat_id, report)
    log_event("self_review_sent", chat_id=chat_id)
    return True
