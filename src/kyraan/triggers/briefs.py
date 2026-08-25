"""Morning brief — the second proactive trigger (plan §2: "reminders,
briefs"). Composed deterministically from live data: no model call, so a
proactive message can never hallucinate an event or a reminder. Gated by
kernel.can_send_proactively() (kill switch + DND) like every proactive
send; a blocked brief is skipped and logged, not queued — the next one
comes tomorrow, and stale "good morning" messages at noon help nobody.
"""
from datetime import time, timedelta

from kyraan.control_plane import config, kernel
from kyraan.control_plane.dnd import humanize, local_now
from kyraan.control_plane.logging_setup import log_event
from kyraan.triggers import scheduler, store


def brief_time() -> time | None:
    """Configured send time, or None when the brief is disabled."""
    cfg = (config.load().get("briefs") or {}).get("morning") or {}
    if not cfg.get("enabled"):
        return None
    hh, mm = str(cfg.get("time", "07:30")).split(":")
    return time(int(hh), int(mm))


async def compose(chat_id: int) -> str:
    now = local_now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    lines = [f"🌅 Morning brief — {now.strftime('%A %d %b')}"]

    try:
        events = await kernel.run_tool(kernel.ToolCall(
            "calendar.list_events",
            {"start": day_start.isoformat(), "end": day_end.isoformat()},
        ))
        if events:
            lines.append("")
            lines.append("Calendar today:")
            for e in events:
                when = "all day" if e["all_day"] else humanize(e["start"])
                where = f" ({e['location']})" if e.get("location") else ""
                lines.append(f"- {when} — {e['title']}{where}")
        else:
            lines.append("")
            lines.append("Nothing on the calendar today.")
    except kernel.ToolFailed as exc:
        # The brief still goes out — an honest gap beats no brief.
        lines.append("")
        lines.append(f"Couldn't check the calendar: {exc}")

    todays = []
    for r in store.list_pending(chat_id):
        try:
            when = scheduler._parse_when(r.when_iso)
        except ValueError:
            continue  # corrupted record — init() already logged it
        if day_start <= when < day_end:
            todays.append((when, r.text))
    if todays:
        lines.append("")
        lines.append("Reminders today:")
        for when, text in sorted(todays):
            lines.append(f"- {humanize(when)} — {text}")
    else:
        lines.append("")
        lines.append("No reminders today.")

    # Home line — best-effort: worth a "the AC ran all night" heads-up,
    # never worth blocking the brief when HA is unreachable/unconfigured.
    try:
        ac = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": "switch.ac"}))
        if ac["state"] == "on":
            try:
                power = await kernel.run_tool(
                    kernel.ToolCall("home.get_state", {"entity": "sensor.ac_current_consumption"})
                )
                lines.append("")
                lines.append(f"⚡ The AC is ON — drawing {power['state']} {power['unit'] or 'W'}.")
            except kernel.ToolFailed:
                lines.append("")
                lines.append("⚡ The AC is ON.")
    except kernel.ToolFailed:
        pass

    return "\n".join(lines)


async def fire(chat_id: int, send_fn) -> bool:
    """Compose and send one brief. Returns False when the proactive gate
    (kill switch or DND) blocked it — skipped, logged, never queued."""
    if not kernel.can_send_proactively():
        log_event("brief_skipped", chat_id=chat_id)
        return False
    text = await compose(chat_id)
    await send_fn(chat_id, text)
    log_event("brief_sent", chat_id=chat_id)
    return True
