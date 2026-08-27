"""Structured, append-only logging for every decision and tool call.

One JSON line per event so the log is greppable and reconstructable later —
"trace why" should always mean "read this file," never "inspect the model."

Turn correlation (2026-08-26): every user message opens a TURN — a
contextvar id stamped onto every event and trace record produced while
handling it, so the complete flow (user text → each model decision →
each tool call → final reply) reconstructs with one grep. Full prompt
and response TEXT goes to traces.jsonl (big records, same rotation, same
local-disk-only boundary as everything else); events.jsonl stays the
compact audit trail.
"""
import contextvars
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import os as _os

# KYRAAN_LOG_DIR redirects ALL logging — scratch scripts and benchmarks
# MUST set it (a catch-up benchmark once sprayed 220k synthetic events
# into the production audit trail, 2026-08-27); pytest isolates via
# conftest, but nothing isolated bare scripts until this knob.
LOG_DIR = Path(_os.environ.get("KYRAAN_LOG_DIR", "") or
               Path(__file__).resolve().parents[3] / "logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
# Rotated archives live out of the way; the top level holds only the
# LIVE files (events/traces/chat + process logs).
ARCHIVE_DIR = LOG_DIR / "archive"
EVENT_LOG = LOG_DIR / "events.jsonl"
# Full-text flow traces: assembled prompts, raw model responses, turn
# start/end. Separate file because one turn can be tens of KB.
TRACE_LOG = LOG_DIR / "traces.jsonl"

_turn_id: contextvars.ContextVar = contextvars.ContextVar("turn_id", default=None)


_turn_stages: contextvars.ContextVar = contextvars.ContextVar("turn_stages", default=None)

# How many `with stage(...)` blocks enclose the current code. Stamped on
# every stage record so consumers can tell a top-level step from work
# nested inside another timed step — summing a flat list double-counted
# (the extraction stage CONTAINS its model calls; trace.py showed >100%).
_stage_depth: contextvars.ContextVar = contextvars.ContextVar("stage_depth", default=0)


def new_turn() -> str:
    """Open a turn: returns the id now stamped on this task's events."""
    tid = uuid.uuid4().hex[:12]
    _turn_id.set(tid)
    _turn_stages.set([])
    return tid


def turn_id():
    return _turn_id.get()


def record_stage(name: str, ms: float, **fields) -> None:
    """One timed pipeline step inside the current turn — model calls,
    tool runs, photo downloads, face matching, geocoding, extraction.
    Collected for the turn_end summary AND written as a trace record, so
    'which step was slow?' is answerable per turn from the log alone."""
    entry = {"stage": name, "ms": round(ms), "depth": _stage_depth.get(),
             **fields}
    stages = _turn_stages.get()
    if stages is not None:
        stages.append(entry)
    log_trace("stage", **entry)


class stage:
    """Context manager: `with logging_setup.stage("face_recognize"): ...`
    times the block (awaits included — wall time is the point)."""

    def __init__(self, name: str, **fields):
        self.name, self.fields = name, fields

    def __enter__(self):
        import time as _t
        self._t0 = _t.monotonic()
        self._depth_token = _stage_depth.set(_stage_depth.get() + 1)
        return self

    def __exit__(self, *exc):
        import time as _t
        # reset FIRST so this stage records at its own level, with only
        # the work inside it counted one level deeper
        _stage_depth.reset(self._depth_token)
        record_stage(self.name, (_t.monotonic() - self._t0) * 1000, **self.fields)
        return False


def turn_summary() -> dict:
    """Aggregates for turn_end: every stage with its ms, plus model-call
    and tool totals — the complete per-turn picture in one record."""
    stages = _turn_stages.get() or []
    model = [s for s in stages if s["stage"].startswith("model:")]
    tools = [s for s in stages if s["stage"].startswith("tool:")]
    return {
        "stages": stages,
        "model_calls": len(model),
        "model_ms": sum(s["ms"] for s in model),
        "tool_ms": sum(s["ms"] for s in tools),
    }
# Full chat transcript (user messages, replies, proactive sends) — the raw
# material for debugging misroutes and, later, Phase 4's reflection loop
# and eval harness. Local disk only, rotated like the event log. Replies
# are logged in full here (including email summaries kept out of MODEL
# context) — the boundary is about third-party models, not the owner's
# own disk.
CHAT_LOG = LOG_DIR / "chat.jsonl"

# Rotate rather than truncate: this file is the audit trail ("trace why"
# always means reading it), so old events are archived beside it, never
# deleted. The size check is one stat() per event — negligible at Kyraan's
# volume — and because every write opens the file fresh, rotation by any
# process is picked up by all of them on their next event.
_ROTATE_BYTES = 5 * 1024 * 1024
# Daily rotation (2026-08-27): traces alone run 5-10MB/day, so live files
# hold only TODAY and everything older lands in logs/archive/YYYY-MM-DD/.
# chat.jsonl is deliberately excluded — restart history-seeding reads it
# ACROSS midnight; cutting it daily would make a morning restart forget
# last night's conversation.
_DAILY_ROTATED = ("events.jsonl", "traces.jsonl")


def _local_date(ts: float | None = None):
    import os as _os
    import time as _time
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(_os.environ.get("KYRAAN_TIMEZONE", "UTC"))
    except Exception:
        tz = timezone.utc
    when = datetime.fromtimestamp(ts if ts is not None else _time.time(), tz)
    return when.date()


def _rotate(path: Path, day) -> None:
    """Archive the live file into its DAY's folder. The uuid suffix keeps
    two processes rotating in the same second from colliding (review P2);
    a lost rename race is harmless."""
    import uuid
    day_dir = ARCHIVE_DIR / day.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        path.rename(day_dir / f"{path.stem}-{stamp}-{uuid.uuid4().hex[:6]}.jsonl")
    except FileNotFoundError:
        pass  # the other process rotated first


def _prune_old_archives(stem: str) -> None:
    # Retention: rotated archives older than 90 days are pruned — they
    # were accumulating forever (completion pack). Day folders empty
    # after pruning are removed too.
    import time as _time
    cutoff = _time.time() - 90 * 86400
    for archive in ARCHIVE_DIR.rglob(f"{stem}-*.jsonl"):
        try:
            if archive.stat().st_mtime < cutoff:
                archive.unlink()
                if archive.parent != ARCHIVE_DIR and not any(archive.parent.iterdir()):
                    archive.parent.rmdir()
        except OSError:
            pass


def _rotate_if_large(path: Path) -> None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    file_day = _local_date(stat.st_mtime)
    today = _local_date()
    if path.name in _DAILY_ROTATED and file_day < today:
        # First write of a new local day: yesterday's records archive
        # under yesterday's folder, however small the file is.
        _prune_old_archives(path.stem)
        _rotate(path, file_day)
        return
    if stat.st_size < _ROTATE_BYTES:
        return
    _prune_old_archives(path.stem)
    _rotate(path, today)


def _append(path: Path, record: dict) -> None:
    import os

    _rotate_if_large(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    # O_CREAT with mode 0600: the file is owner-only from the moment it
    # exists — no umask window, and never a byte of personal data before
    # hardening (security round 5; a post-rotation replacement inherited
    # the process umask and was chmodded only after the first write).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# Health layer (2026-08-27): event kinds that mean SOMETHING WENT WRONG
# this turn — not rails doing their job (a stage-scope block or web-taint
# lock is enforcement, not anomaly), but failures, fallbacks, deferrals,
# and corrections. The per-turn collector below tags each turn with the
# set it saw; turn_health events make "was this turn clean?" a query.
ANOMALY_KINDS = frozenset({
    "model_call_error", "agent_tier_fallback", "agent_loop_error",
    "agent_all_tiers_failed", "agent_false_success_corrected",
    "agent_deflection_corrected", "agent_referent_corrected",
    "tool_loop_detected",
    "agent_tool_error", "handle_message_error",
    "extraction_skipped_slow", "extraction_error",
    "fact_sync_deferred", "promise_sync_deferred",
    "session_backend_fallback", "memory_backend_fallback",
    "promises_backend_fallback", "memory_visibility_failclosed",
    "episode_suppress_deferred", "episode_tagging_failed",
    "episode_tagging_cloud_failed", "triple_extract_deferred",
    "document_ingest_failed", "episode_rag_skipped",
    "document_rag_skipped", "confirmation_restore_failed",
    "action_log_failed", "face_sync_deferred", "person_lookup_failed",
    "undo_store_unreachable", "consolidation_scan_failed",
    "photo_vision_unavailable", "provider_cooldown",
    "budget_exhausted", "person_budget_exhausted",
    "token_guard_blocked", "pg_mirror_stale", "auto_approve_failed",
    "nightly_stage_failed", "pending_purge_failed", "event_rule_error",
    "reply_delivery_failed",
})

_turn_anomalies: contextvars.ContextVar = contextvars.ContextVar(
    "turn_anomalies", default=None)


def start_anomaly_capture():
    return _turn_anomalies.set([])


def collected_anomalies() -> list:
    return list(_turn_anomalies.get() or [])


def log_event(kind: str, **fields) -> None:
    """Append one structured event, e.g. kind='tool_call', kind='routing_decision'."""
    tid = _turn_id.get()
    if kind in ANOMALY_KINDS:
        bucket = _turn_anomalies.get()
        if bucket is not None:
            bucket.append(kind)
    _append(EVENT_LOG, {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind,
                        **({"turn_id": tid} if tid else {}), **fields})


def log_trace(kind: str, **fields) -> None:
    """Append one full-text trace record (prompts, responses, turn
    boundaries) — traces.jsonl, correlated to events by turn_id."""
    tid = _turn_id.get()
    _append(TRACE_LOG, {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind,
                        **({"turn_id": tid} if tid else {}), **fields})


def log_chat(chat_id: int, role: str, text: str, **fields) -> None:
    """One transcript line: role is 'user', 'assistant', or 'proactive'."""
    _append(CHAT_LOG, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id, "role": role, "text": text, **fields,
    })


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
