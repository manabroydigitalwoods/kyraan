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

# Rotate rather than truncate: this file is the audit trail ("trace why"
# always means reading it), so old events are archived beside it, never
# deleted. The size check is one stat() per event — negligible at Kyraan's
# volume — and because every write opens the file fresh, rotation by any
# process is picked up by all of them on their next event.
_ROTATE_BYTES = 5 * 1024 * 1024


def _rotate_if_large() -> None:
    try:
        if EVENT_LOG.stat().st_size < _ROTATE_BYTES:
            return
    except FileNotFoundError:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    EVENT_LOG.rename(EVENT_LOG.with_name(f"events-{stamp}.jsonl"))


def log_event(kind: str, **fields) -> None:
    """Append one structured event, e.g. kind='tool_call', kind='routing_decision'."""
    _rotate_if_large()
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        **fields,
    }
    with EVENT_LOG.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
