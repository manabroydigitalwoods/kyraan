"""Kyraan's self-knowledge, generated — never hand-written prose.

Every false capability denial and tone-deaf answer today traced to the
same root: the qa prompt described abilities in hand-maintained sentences
that drifted from what was actually built and configured. This module
derives the capability brief from config + environment at answer time, so
a new tool, a missing credential, or a grown allowlist updates Kyraan's
self-description automatically. The brief feeds the qa prompt's
"what you can and can't do" section — the model mirrors a human assistant
exactly as far as this list is true, and no further.
"""
import os

from kyraan.control_plane import config


def _has_env(*keys: str) -> bool:
    return all(os.environ.get(k, "").strip() for k in keys)


def _friendly_entity(entity: str) -> str:
    return entity.split(".", 1)[1].replace("_", " ")


def capability_brief() -> str:
    lines = ["THINGS YOU CAN DO (live right now):"]
    lines.append('- Reminders: create, list, cancel; they arrive as Telegram messages ("remind me to X at 7pm").')
    lines.append("- Remember stated personal facts (they go live after the owner reviews them) and recall reviewed ones.")
    lines.append("- General Q&A, writing, code — from your own knowledge.")
    lines.append('- Report your own AI usage and spend ("how much did we spend this week?", "are we near the budget?").')

    not_connected = []

    if _has_env("GOOGLE_CALENDAR_ICS_URL"):
        lines.append('- Read the Google Calendar ("what\'s on my calendar tomorrow?").')
    else:
        not_connected.append("Calendar reading (needs the calendar's secret ICS URL)")
    if _has_env("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"):
        lines.append('- Create calendar events — each one needs the owner\'s explicit yes ("add lunch friday 1pm to my calendar").')
        lines.append('- Cancel/delete calendar events — the exact events are named and need the owner\'s explicit yes ("cancel the 3pm meeting").')
        lines.append('- Check unread email: senders and subjects ONLY, never message bodies (a deliberate privacy boundary — say so if asked to open/summarize an email, and point to Gmail).')
    else:
        not_connected.append("Calendar event creation and email checking (needs the Google OAuth setup)")

    if _has_env("HASS_URL", "HASS_TOKEN"):
        server = (config.load().get("tool_servers") or {}).get("home_assistant") or {}
        sensors = [e for e in (server.get("read_entities") or []) if e.startswith("sensor.")]
        switches = server.get("write_entities") or []
        if switches:
            names = ", ".join(_friendly_entity(e) for e in switches)
            lines.append(f"- Check and switch these smart plugs (switching needs the owner's yes): {names}.")
        if any("temp" in e or "humid" in e for e in sensors):
            lines.append("- Read the bedroom temperature/humidity, plug power and energy use.")
    else:
        not_connected.append("Smart home (needs the Home Assistant URL + token)")

    from kyraan.triggers.briefs import brief_time  # lazy: avoids import order issues
    at = brief_time()
    if at is not None:
        lines.append(f"- A morning brief arrives daily at {at.strftime('%H:%M')} (calendar, reminders, home status).")
    lines.append("")
    lines.append(
        "YOU HAVE NO INTERNET ACCESS: no web search, no browsing, no news, no "
        "live data of any kind — your tools reach only the calendar, email "
        "metadata, and home devices listed above, and your general knowledge "
        "ends at your training cutoff. Never claim to look anything up online."
    )
    # The privacy answer must track the ACTUAL tier config — after the
    # 2026-08-26 switch to local-only qwen3, "Groq's cloud API" would have
    # been a false claim in the other direction.
    tiers = config.load().get("model_tiers", {})
    tier_providers = {t.get("provider") for t in tiers.values()}
    if tier_providers <= {"ollama"}:
        model_truth = (
            "conversation text is processed entirely by a LOCAL AI model on "
            "this same computer — no conversation ever leaves the machine"
        )
    else:
        cloud = ", ".join(sorted(p for p in tier_providers if p != "ollama"))
        model_truth = (
            "conversation text is processed by the configured AI models (a "
            f"local one and the {cloud} cloud API) to generate replies"
        )
    lines.append(
        "IF ASKED ABOUT DATA OR PRIVACY, answer with exactly these truths: "
        "everything runs on the owner's own computer; facts you're told are "
        f"stored as local files only after the owner reviews them; {model_truth}; "
        "nothing is ever used to train models; "
        "email bodies are never read; nothing is shared with anyone else."
    )

    if not_connected:
        lines.append("")
        lines.append("SET UP BUT NOT CONNECTED YET (say what's missing, in one line, if asked):")
        lines.extend(f"- {item}" for item in not_connected)

    lines.append("")
    lines.append(
        "EVERYTHING ELSE — web browsing, bookings, calls, music, payments, "
        "opening email bodies, editing/rescheduling calendar events, "
        "GENERATING OR VIEWING IMAGES (you cannot create, draft, see, or "
        "analyze any image or photo — do not offer to), voice notes, devices not "
        "listed above — you can NOT do yet. When asked, say so plainly in one "
        "short line, like a capable human assistant would (\"I can't book cabs "
        "yet\") — no apology spiral, no inventing abilities, and offer an "
        "alternative only when a listed capability genuinely helps (for an "
        "image request: you can write a detailed prompt for an image tool, "
        "but say clearly the image itself must be made elsewhere)."
    )
    return "\n".join(lines)
