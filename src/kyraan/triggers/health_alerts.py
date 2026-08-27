"""Warning lights (owner: anomaly detection, 2026-08-27). The flight
recorder (events.jsonl) already sees every failure; this layer decides
when one deserves the owner's ATTENTION — as a one-line note appended
to the reply of the turn that crossed the line (in-band: no separate
send machinery, inherently DND-free, and it names the turn that was
actually degraded).

Deterministic and throttled: CRITICAL kinds alert on first sight,
RATE kinds on >= BURST_N occurrences within BURST_WINDOW_S; each kind
alerts at most once per local day (persisted — a restart can't re-spam).
"""
import json
import time
from pathlib import Path

from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.filelock import atomic_write_text, locked
from kyraan.control_plane.logging_setup import log_event

STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "health_alerts.json"

# First occurrence is already actionable:
CRITICAL = {
    "agent_all_tiers_failed": "both reasoning models were unreachable",
    "budget_exhausted": "the daily model budget is exhausted",
    "handle_message_error": "a message handler crashed (see events.jsonl)",
    "memory_visibility_failclosed": "a non-owner viewer got no facts (pg trouble)",
    "pg_mirror_stale": "a Postgres mirror is behind the files",
}
# A pattern, not a blip:
RATE = {
    "model_call_error": "the model provider keeps erroring",
    "agent_tier_fallback": "replies keep falling to the local model",
    "agent_false_success_corrected": "the model keeps claiming unperformed actions",
    "extraction_skipped_slow": "memory extraction keeps timing out",
    "session_backend_fallback": "Redis session state fell back to memory",
    "fact_sync_deferred": "fact mirroring to Postgres keeps failing",
    "promise_sync_deferred": "reminder mirroring to Postgres keeps failing",
}
BURST_N = 3
BURST_WINDOW_S = 30 * 60

_recent: dict = {}  # kind -> [monotonic timestamps]


def _alerted_today(kind: str) -> bool:
    try:
        state = json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}
    return state.get(kind) == local_now().date().isoformat()


def _mark_alerted(kind: str) -> None:
    with locked(STATE_PATH):
        try:
            state = json.loads(STATE_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = {}
        state[kind] = local_now().date().isoformat()
        STATE_PATH.parent.mkdir(exist_ok=True)
        atomic_write_text(STATE_PATH, json.dumps(state, indent=1))


def check(anomalies: list) -> str | None:
    """Called once per turn with that turn's anomaly kinds. Returns ONE
    warning line to append to the reply, or None."""
    now = time.monotonic()
    fired = []
    for kind in anomalies:
        if kind in CRITICAL:
            if not _alerted_today(kind):
                fired.append((kind, CRITICAL[kind]))
        elif kind in RATE:
            stamps = [t for t in _recent.get(kind, []) if now - t < BURST_WINDOW_S]
            stamps.append(now)
            _recent[kind] = stamps
            if len(stamps) >= BURST_N and not _alerted_today(kind):
                fired.append((kind, RATE[kind]))
    if not fired:
        return None
    kind, description = fired[0]  # one line; the rest alert on later turns
    _mark_alerted(kind)
    log_event("health_alert", alert=kind)
    return (f"\n\n⚠️ Health: {description} — say \"health report\" for the "
            "full picture. (One note per issue per day.)")
