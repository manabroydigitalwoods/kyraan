"""Usage analytics over the model-call audit trail — the numbers behind
"how much are we spending and on what?".

Sources, both already durable:
- events.jsonl model_call entries (input/output/cached tokens, cost,
  model, tier — per call), including rotated files for week-scale views
- the cost ledger (budget config + authoritative daily spend)

Exposed to the owner as the agent tool `usage.report`, so the analysis
lives where the question is asked: in chat.
"""
import json
from collections import defaultdict
from datetime import timedelta

from kyraan.control_plane import logging_setup
from kyraan.control_plane.dnd import local_now
from kyraan.model_router import router


def _event_files():
    """Live log plus rotated archives. Rotation moves files into
    logs/archive/<day>/ — globbing the log's own directory found nothing,
    so any window longer than today was silently incomplete (Bugbot P2)."""
    log = logging_setup.EVENT_LOG
    archive = logging_setup.ARCHIVE_DIR
    rotated = list(archive.rglob(f"{log.stem}-*.jsonl")) if archive.exists() else []
    # Pre-archive installs rotated BESIDE the live log; read both layouts
    # so no window is silently short.
    rotated += list(log.parent.glob(f"{log.stem}-*.jsonl"))
    return [p for p in (*sorted(set(rotated)), log) if p.exists()]


def usage_summary(days: int = 7) -> dict:
    """Per-local-day rollup of model calls for the last `days` days, plus
    the live budget picture."""
    days = max(1, min(int(days), 31))
    today = local_now().date()
    cutoff = today - timedelta(days=days - 1)
    tz = local_now().tzinfo

    daily: dict = defaultdict(lambda: {
        "calls": 0, "input_tokens": 0, "output_tokens": 0,
        "cached_tokens": 0, "cost_usd": 0.0, "by_model": defaultdict(int),
    })
    from datetime import datetime
    for path in _event_files():
        for line in path.read_text(errors="replace").splitlines():
            if '"model_call"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("kind") != "model_call":
                continue
            try:
                day = datetime.fromisoformat(event["ts"]).astimezone(tz).date()
            except (KeyError, ValueError):
                continue
            if day < cutoff or day > today:
                continue
            row = daily[day.isoformat()]
            row["calls"] += 1
            row["input_tokens"] += event.get("input_tokens") or 0
            row["output_tokens"] += event.get("output_tokens") or 0
            row["cached_tokens"] += event.get("cached_tokens") or 0
            row["cost_usd"] = round(row["cost_usd"] + (event.get("cost_usd") or 0), 6)
            row["by_model"][event.get("model", "?")] += 1

    for row in daily.values():
        row["by_model"] = dict(row["by_model"])

    budget = router.daily_budget_usd()
    spent = router.today_cost_usd()
    return {
        "days": [{"date": d, **daily[d]} for d in sorted(daily)],
        "budget": {
            "daily_budget_usd": budget,
            "spent_today_usd": round(spent, 4),
            "budget_used_pct": round(spent / budget * 100, 1) if budget > 0 else None,
            "alert_threshold_pct": router.budget_alert_threshold_pct(),
        },
    }


def recent_turns(n: int = 8) -> list:
    """Per-MESSAGE spend (owner asked twice, 2026-09-01): the last n
    turns' model calls grouped by turn_id, joined with the turn's user
    text from the trace log — cost, tokens, calls, cache share."""
    import collections
    turns: "collections.OrderedDict" = collections.OrderedDict()
    for path in _event_files():
        for line in path.read_text().splitlines():
            if '"model_call"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = e.get("turn_id")
            if not tid:
                continue
            t = turns.setdefault(tid, {"ts": e["ts"][11:16], "calls": 0,
                                       "in": 0, "cached": 0, "out": 0,
                                       "usd": 0.0})
            t["calls"] += 1
            t["in"] += e.get("input_tokens") or 0
            t["cached"] += e.get("cached_tokens") or 0
            t["out"] += e.get("output_tokens") or 0
            t["usd"] += e.get("cost_usd") or 0
    texts = {}
    trace = logging_setup.TRACE_LOG
    if trace.exists():
        for line in trace.read_text().splitlines():
            if '"turn_start"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("turn_id"):
                texts[e["turn_id"]] = str(e.get("user_text") or "")[:60]
    out = []
    for tid, t in list(turns.items())[-n:]:
        out.append({**t, "usd": round(t["usd"], 4),
                    "text": texts.get(tid, "(proactive/scheduled)")})
    return out
