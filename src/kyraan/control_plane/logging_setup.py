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

LOG_DIR = Path(__file__).resolve().parents[3] / "logs"
LOG_DIR.mkdir(exist_ok=True)
EVENT_LOG = LOG_DIR / "events.jsonl"
# Full-text flow traces: assembled prompts, raw model responses, turn
# start/end. Separate file because one turn can be tens of KB.
TRACE_LOG = LOG_DIR / "traces.jsonl"

_turn_id: contextvars.ContextVar = contextvars.ContextVar("turn_id", default=None)


_turn_stages: contextvars.ContextVar = contextvars.ContextVar("turn_stages", default=None)


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
    entry = {"stage": name, "ms": round(ms), **fields}
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
        return self

    def __exit__(self, *exc):
        import time as _t
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


def _rotate_if_large(path: Path) -> None:
    try:
        if path.stat().st_size < _ROTATE_BYTES:
            return
    except FileNotFoundError:
        return
    import time as _time
    import uuid
    # Retention: rotated archives older than 90 days are pruned — they
    # were accumulating forever (completion pack).
    cutoff = _time.time() - 90 * 86400
    for archive in path.parent.glob(f"{path.stem}-*.jsonl"):
        try:
            if archive.stat().st_mtime < cutoff:
                archive.unlink()
        except OSError:
            pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        # uuid suffix: two processes rotating in the same second produced
        # colliding archive names (review P2); a lost race is harmless.
        path.rename(path.with_name(f"{path.stem}-{stamp}-{uuid.uuid4().hex[:6]}.jsonl"))
    except FileNotFoundError:
        pass  # the other process rotated first


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


def log_event(kind: str, **fields) -> None:
    """Append one structured event, e.g. kind='tool_call', kind='routing_decision'."""
    tid = _turn_id.get()
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
