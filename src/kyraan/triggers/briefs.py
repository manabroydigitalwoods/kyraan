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


def brief_time(which: str = "morning") -> time | None:
    """Configured send time for a brief, or None when disabled."""
    default = "07:30" if which == "morning" else "21:30"
    cfg = (config.load().get("briefs") or {}).get(which) or {}
    if not cfg.get("enabled"):
        return None
    hh, mm = str(cfg.get("time", default)).split(":")
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

    # Home lines — best-effort: worth a "the AC ran all night" heads-up,
    # never worth blocking the brief when HA is unreachable/unconfigured.
    home = []
    try:
        temp = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": "sensor.bed_room_temp_temperature"}))
        humidity = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": "sensor.bed_room_temp_humidity"}))
        home.append(f"🌡 Bedroom {temp['state']}{temp['unit'] or '°C'} / {humidity['state']}{humidity['unit'] or '%'}")
    except kernel.ToolFailed:
        pass
    try:
        ac = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": "switch.ac"}))
        if ac["state"] == "on":
            try:
                power = await kernel.run_tool(
                    kernel.ToolCall("home.get_state", {"entity": "sensor.ac_current_consumption"})
                )
                home.append(f"⚡ The AC is ON — drawing {power['state']} {power['unit'] or 'W'}.")
            except kernel.ToolFailed:
                home.append("⚡ The AC is ON.")
    except kernel.ToolFailed:
        pass
    if home:
        lines.append("")
        lines.extend(home)

    # Important-email digest (email tools enhancement, 2026-08-28) —
    # deterministic, unread-only, composed in Python: sender/subject
    # NEVER touches a model prompt on the way into this line, same §3a
    # boundary as every other email surface. Best-effort like home.
    try:
        result = await kernel.run_tool(kernel.ToolCall(
            "email.important", {"limit": 3}))
        items = result.get("messages", [])
        if items:
            lines.append("")
            lines.append(f"📬 {len(items)} important unread:")
            for m in items:
                sender = str(m.get("from", "?")).split("<")[0].strip().strip('"')
                lines.append(f"- {sender}: {m.get('subject', '(no subject)')}")
    except kernel.ToolFailed:
        pass
    except Exception:
        pass

    # Curiosity (Phase 4, 2026-08-28): at most one deterministic
    # knowledge-gap question a day, riding the brief — batched and
    # DND-safe by construction. Best-effort like the home lines.
    try:
        from kyraan.triggers import curiosity
        question = curiosity.daily_line(chat_id)
        if question:
            lines.append("")
            lines.append(question)
    except Exception:
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


async def compose_evening(chat_id: int) -> str:
    """The day's bookend: tomorrow's calendar, tomorrow's reminders, and
    today's energy story — deterministic, same rules as the morning."""
    now = local_now()
    tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_end = tomorrow_start + timedelta(days=1)
    lines = [f"🌙 Evening brief — {now.strftime('%A %d %b')}"]

    try:
        events = await kernel.run_tool(kernel.ToolCall(
            "calendar.list_events",
            {"start": tomorrow_start.isoformat(), "end": tomorrow_end.isoformat()},
        ))
        lines.append("")
        if events:
            lines.append("Tomorrow:")
            for e in events:
                when = "all day" if e["all_day"] else humanize(e["start"])
                where = f" ({e['location']})" if e.get("location") else ""
                lines.append(f"- {when} — {e['title']}{where}")
        else:
            lines.append("Nothing on tomorrow's calendar.")
    except kernel.ToolFailed as exc:
        lines.append("")
        lines.append(f"Couldn't check the calendar: {exc}")

    tomorrows = []
    for r in store.list_pending(chat_id):
        try:
            when = scheduler._parse_when(r.when_iso)
        except ValueError:
            continue
        if tomorrow_start <= when < tomorrow_end:
            tomorrows.append((when, r.text))
    if tomorrows:
        lines.append("")
        lines.append("Reminders tomorrow:")
        for when, text in sorted(tomorrows):
            lines.append(f"- {humanize(when)} — {text}")

    home = []
    try:
        energy = await kernel.run_tool(kernel.ToolCall(
            "home.get_state", {"entity": "sensor.ac_today_s_consumption"}))
        home.append(f"⚡ AC used {energy['state']} {energy['unit'] or 'kWh'} today.")
    except kernel.ToolFailed:
        pass
    try:
        ac = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": "switch.ac"}))
        if ac["state"] == "on":
            home.append("The AC is still ON.")
    except kernel.ToolFailed:
        pass
    if home:
        lines.append("")
        lines.extend(home)

    return "\n".join(lines)


async def fire_evening(chat_id: int, send_fn) -> bool:
    if not kernel.can_send_proactively():
        log_event("evening_brief_skipped", chat_id=chat_id)
        return False
    text = await compose_evening(chat_id)
    await send_fn(chat_id, text)
    log_event("evening_brief_sent", chat_id=chat_id)
    return True
