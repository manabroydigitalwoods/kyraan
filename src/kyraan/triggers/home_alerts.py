"""Proactive home intelligence — Kyraan noticing things on its own.

Deterministic like the briefs (no model call: a proactive message can
never hallucinate a wattage), gated by kernel.can_send_proactively()
like every proactive send. Two rules, both config-tunable and both
once-per-occurrence (markers live in data/home_alerts.json so a restart
can't re-alert):

- AC continuous runtime: on for longer than `ac_max_hours` (per HA's own
  last_changed for the current ON stretch) -> one alert per stretch.
- Daily energy: today's AC consumption crossing `daily_kwh_alert` ->
  one alert per day.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from kyraan.control_plane import config, kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.filelock import atomic_write_text, locked
from kyraan.control_plane.logging_setup import log_event

MARKERS_PATH = Path(__file__).resolve().parents[3] / "data" / "home_alerts.json"


def _cfg() -> dict:
    return (config.load().get("home_alerts") or {})


def enabled() -> bool:
    return bool(_cfg().get("enabled"))


def _markers() -> dict:
    try:
        return json.loads(MARKERS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _mark(key: str, value: str) -> None:
    with locked(MARKERS_PATH):
        markers = _markers()
        markers[key] = value
        MARKERS_PATH.parent.mkdir(exist_ok=True)
        atomic_write_text(MARKERS_PATH, json.dumps(markers, indent=1))


def decide_alerts(ac: dict, energy: dict | None, markers: dict, now=None) -> list:
    """Pure decision logic (tested directly): given the AC switch state,
    today's-energy sensor reading, and the sent-markers, return the alert
    texts due now, with their marker updates as (key, value, text)."""
    settings = _cfg()
    now = now or datetime.now(timezone.utc)
    due = []

    max_hours = float(settings.get("ac_max_hours", 6))
    if ac.get("state") == "on" and ac.get("last_changed"):
        try:
            since = datetime.fromisoformat(str(ac["last_changed"]).replace("Z", "+00:00"))
            hours = (now - since).total_seconds() / 3600
        except (ValueError, TypeError):
            hours = 0
        stretch_id = str(ac["last_changed"])
        if hours >= max_hours and markers.get("ac_stretch") != stretch_id:
            due.append(("ac_stretch", stretch_id,
                        f"⚡ Heads-up: the AC has been running for about "
                        f"{hours:.0f} hours straight. Want it off? Just say so."))

    kwh_limit = float(settings.get("daily_kwh_alert", 8.0))
    if energy is not None:
        try:
            kwh = float(energy.get("state"))
        except (TypeError, ValueError):
            kwh = 0.0
        day_key = local_now().date().isoformat()
        if kwh >= kwh_limit and markers.get("kwh_day") != day_key:
            due.append(("kwh_day", day_key,
                        f"⚡ Today's AC consumption just crossed {kwh:.1f} kWh "
                        f"(your alert level is {kwh_limit:g})."))
    return due


async def check(chat_id: int, send_fn) -> int:
    """One periodic pass. Returns how many alerts were sent."""
    if not enabled() or not kernel.can_send_proactively():
        return 0
    try:
        ac = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": "switch.ac"}))
    except kernel.ToolFailed:
        return 0  # HA unreachable — silence, not noise; next pass retries
    energy = None
    try:
        energy = await kernel.run_tool(kernel.ToolCall(
            "home.get_state", {"entity": "sensor.ac_today_s_consumption"}))
    except kernel.ToolFailed:
        pass

    sent = 0
    for key, value, text in decide_alerts(ac, energy, _markers()):
        await send_fn(chat_id, text)
        _mark(key, value)
        log_event("home_alert_sent", rule=key, chat_id=chat_id)
        sent += 1
    return sent
