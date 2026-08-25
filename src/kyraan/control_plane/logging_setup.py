"""Structured, append-only logging for every decision and tool call.

One JSON line per event so the log is greppable and reconstructable later —
"trace why" should always mean "read this file," never "inspect the model."
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[3] / "logs"
LOG_DIR.mkdir(exist_ok=True)
EVENT_LOG = LOG_DIR / "events.jsonl"
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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path.rename(path.with_name(f"{path.stem}-{stamp}.jsonl"))


def _append(path: Path, record: dict) -> None:
    _rotate_if_large(path)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def log_event(kind: str, **fields) -> None:
    """Append one structured event, e.g. kind='tool_call', kind='routing_decision'."""
    _append(EVENT_LOG, {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields})


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
