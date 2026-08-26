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
    for line in raw[-3000:]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
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
    return f"🔍 Daily self-review\n\n{report}" if report else ""


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
