"""House steward — duty #2 (owner 2026-09-03, "go house steward").

What it owns, on top of the threshold alerts that already exist
(temperature, PM2.5, AC overheat/overload):

  * PURIFIER FILTERS: the NanoProtect filter and the pre-filter report
    life in %. Below 20% Kyraan says so once (reorder / wash), again at
    10%; never more.
  * AC ENERGY BY MONTH: HA's "today" reading is sampled every night into
    a ledger; on the 1st the month is compared with the month before
    ("August: 61 kWh, 30% more than July"). Kyraan keeps the history HA
    forgets.
  * NIGHTLY SETTLE (21:45, before quiet hours): one message, only when
    something is worth a hand — the AC still on and drawing, the
    purifier on turbo/medium with no timer, the display backlight on —
    with the one-word fix for each. Silent when the house is settled.
  * "house status" on demand.

Same proactive gate (kill switch + DND), same delivery truth, one
state file (markers + the energy ledger).
"""
import json
from datetime import date, time, timedelta
from pathlib import Path

from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.filelock import atomic_write_text
from kyraan.control_plane.logging_setup import log_event

STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "duties" / "house_steward.json"
FILTER_WARN = 20
FILTER_URGENT = 10
SETTLE_AC_WATTS = 300      # below this the AC is idle/fan; not worth a line

ENTITIES = {
    "ac": "switch.ac", "ac_w": "sensor.ac_current_consumption",
    "ac_today": "sensor.ac_today_s_consumption",
    "nano": "sensor.air_purifier_nanoprotect_filter", "pre": "sensor.air_purifier_pre_filter",
    "backlight": "light.air_purifier_display_backlight", "pm25": "sensor.air_purifier_pm2_5",
    "temp": "sensor.bed_room_temp_temperature", "humidity": "sensor.bed_room_temp_humidity",
}


def _cfg() -> dict:
    from kyraan.control_plane import config
    return (config.load().get("duties") or {}).get("house_steward") or {}


def settle_time() -> time | None:
    cfg = _cfg()
    if cfg.get("enabled", True) is False:
        return None
    hh, mm = str(cfg.get("settle", "21:45")).split(":")
    return time(int(hh), int(mm))


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"markers": {}, "energy": {}}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False))


async def _read(key: str):
    try:
        st = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": ENTITIES[key]}), meta=True)
        return st.get("state") if isinstance(st, dict) else None
    except Exception:
        return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- filters --

def filter_lines(nano, pre, markers: dict, today: str) -> list:
    """One line per filter crossing 20% / 10%, said once per level."""
    lines = []
    for key, value, what, fix in (("nano", nano, "NanoProtect filter", "order a replacement"),
                                  ("pre", pre, "pre-filter", "take it out and wash it")):
        v = _num(value)
        if v is None:
            continue
        level = "urgent" if v <= FILTER_URGENT else "warn" if v <= FILTER_WARN else ""
        if not level:
            markers.pop(f"filter:{key}", None)     # replaced: arm again
            continue
        if markers.get(f"filter:{key}") == level:
            continue
        lines.append(f"• Purifier {what} at {int(v)}% — {fix}" + (" soon" if level == "warn" else " now") + ".")
        markers[f"filter:{key}"] = level
    return lines


# -------------------------------------------------------------- energy --

def record_energy(state: dict, day: str, kwh) -> None:
    v = _num(kwh)
    if v is not None:
        state.setdefault("energy", {})[day] = round(v, 3)
        keep = sorted(state["energy"])[-120:]
        state["energy"] = {k: state["energy"][k] for k in keep}


def month_line(energy: dict, today: date) -> str:
    """On the 1st: last month vs the month before, from the ledger."""
    if today.day != 1:
        return ""
    last = (today.replace(day=1) - timedelta(days=1))
    prev = (last.replace(day=1) - timedelta(days=1))
    def total(month_start: date) -> float:
        return sum(v for k, v in energy.items() if k[:7] == month_start.strftime("%Y-%m"))
    a, b = total(last), total(prev)
    if not a:
        return ""
    line = f"⚡ AC in {last.strftime('%B')}: {a:.0f} kWh"
    if b:
        change = (a - b) / b * 100
        line += f" — {abs(change):.0f}% {'more' if change > 0 else 'less'} than {prev.strftime('%B')} ({b:.0f} kWh)"
    return line + "."


# -------------------------------------------------------------- settle --

def settle_lines(ac_state, ac_w, purifier: dict, backlight) -> list:
    lines = []
    w = _num(ac_w) or 0
    if ac_state == "on" and w >= SETTLE_AC_WATTS:
        lines.append(f"• AC is on and drawing {w:.0f} W — \"turn off the AC\" if you're done with it.")
    mode = (purifier or {}).get("mode") or ""
    timer = str((purifier or {}).get("timer") or "Off")
    if (purifier or {}).get("power") == "on" and mode in ("turbo", "medium") and timer.lower() == "off":
        lines.append(f"• Purifier is on {mode} with no timer — \"purifier sleep mode\" or \"purifier off in 8 hours\".")
    if backlight == "on":
        lines.append("• Purifier display light is on — \"turn off the purifier display backlight\".")
    return lines


async def status_text() -> str:
    ac, w, today_kwh = await _read("ac"), await _read("ac_w"), await _read("ac_today")
    nano, pre, back = await _read("nano"), await _read("pre"), await _read("backlight")
    temp, hum, pm = await _read("temp"), await _read("humidity"), await _read("pm25")
    try:
        from kyraan.tools import home_assistant as _ha
        import asyncio as _aio
        pur = await _aio.to_thread(_ha.purifier_state)
    except Exception:
        pur = {}
    lines = ["🏠 House right now:"]
    lines.append(f"• Bedroom {temp}°C / {hum}% — PM2.5 {pm}")
    lines.append(f"• AC {ac}" + (f", drawing {_num(w) or 0:.0f} W" if ac == "on" else "") + f" — {today_kwh} kWh today")
    if pur:
        lines.append(f"• Purifier {pur.get('power')}, {pur.get('mode')} mode, timer {pur.get('timer')}, display light {back}")
    lines.append(f"• Filters: NanoProtect {nano}%, pre-filter {pre}%")
    energy = _load().get("energy", {})
    month = local_now().date().strftime("%Y-%m")
    used = sum(v for k, v in energy.items() if k[:7] == month)
    if used:
        lines.append(f"• AC this month so far: {used:.0f} kWh (from my nightly ledger)")
    return "\n".join(lines)


async def fire_settle(chat_id: int, send_fn) -> bool:
    """21:45 nightly: sample the energy ledger, then say only what is
    worth a hand. Filter and month lines ride along when due."""
    state = _load()
    today = local_now().date()
    record_energy(state, today.isoformat(), await _read("ac_today"))
    markers = state.setdefault("markers", {})
    lines = []
    lines += filter_lines(await _read("nano"), await _read("pre"), markers, today.isoformat())
    month = month_line(state.get("energy", {}), today)
    if month and markers.get("month") != today.strftime("%Y-%m"):
        lines.append(month)
        markers["month"] = today.strftime("%Y-%m")
    try:
        from kyraan.tools import home_assistant as _ha
        import asyncio as _aio
        pur = await _aio.to_thread(_ha.purifier_state)
    except Exception:
        pur = {}
    lines += settle_lines(await _read("ac"), await _read("ac_w"), pur, await _read("backlight"))
    _save(state)                    # the ledger sample is kept regardless
    if not lines:
        return False
    if not kernel.can_send_proactively(chat_id=chat_id):
        # keep the filter/month markers unset for tomorrow
        for k in [k for k in markers if k.startswith("filter:")]:
            markers.pop(k, None)
        markers.pop("month", None) if month else None
        _save(state)
        return False
    text = "🏠 Before the night:\n" + "\n".join(lines)
    if await send_fn(chat_id, text) is False:
        for k in [k for k in markers if k.startswith("filter:")]:
            markers.pop(k, None)
        _save(state)
        return False
    _save(state)
    log_event("house_steward_sent", chat_id=chat_id, items=len(lines))
    return True
