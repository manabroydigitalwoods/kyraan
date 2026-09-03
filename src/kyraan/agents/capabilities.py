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


_FRIENDLY = {
    "qa": "answer questions, write, and chat",
    "agent.action": None,  # plumbing, not a user-facing ability
    "reminders": "their reminders: create, list, snooze, cancel",
    "calendar.list_events": "read the household calendar",
    "weather": "live weather and forecasts",
    "places": "find nearby places",
    "routes": "travel times with live traffic",
    "web.search": "search the web",
    "documents.search": "search documents THEY have captured",
    "memory.propose": "have their stated facts remembered (their own review)",
    "memory.review": "review their own pending facts",
    "memory.pending_list": "see their own pending facts",
    "media.photo": "send photos (described and captured as THEIR documents)",
    "media.file": "send PDFs and files into THEIR document memory",
    "media.voice": "send voice notes (transcribed on this machine)",
    "media.location": "share location pins",
}


def viewer_brief(person: str, stage: str) -> str:
    """The capability brief for a NON-OWNER viewer: only what THEY can
    do (2026-08-28: the owner-shaped brief rode into Ruma's prompts,
    advertising email/home/files she doesn't have — her "What access i
    have?" got a confused answer). Built from their effective access:
    stage toolset ∪ the owner's grants."""
    allowed = list((config.load().get("stage_toolsets") or {}).get(stage) or [])
    try:
        from kyraan.store import persons
        allowed += persons.extra_tools(person)
    except Exception:
        pass
    lines = [f"THINGS THIS USER CAN DO here (access stage {stage!r}; "
             "nothing else — never claim or offer an ability outside "
             "this list):"]
    seen = set()
    for entry in allowed:
        friendly = _FRIENDLY.get(entry, _FRIENDLY.get(entry.split(".")[0]))
        if friendly and friendly not in seen:
            seen.add(friendly)
            lines.append(f"- {friendly}")
    lines.append("- ask what they can do here (answer from THIS list)")
    lines.append("They are NOT the owner: no access to the owner's memory, "
                 "email, home control, faces, or files — and access "
                 "changes are the owner's alone.")
    return "\n".join(lines)


def capability_brief() -> str:
    from kyraan.control_plane import kernel as _kernel
    stage = _kernel.viewer_stage()
    if stage not in ("owner",):
        person = _kernel.effective_reviewer() or "unknown"
        return viewer_brief(person, stage)
    lines = ["THINGS YOU CAN DO (live right now):"]
    lines.append("- Remember stated personal facts (they go live after the owner reviews them) and recall reviewed ones.")
    lines.append("- General Q&A, writing, code — from your own knowledge.")
    try:
        from kyraan.channels import voice as _voice
        if _voice.available():
            lines.append("- Understand Telegram voice notes — transcribed locally on this computer; the audio never leaves the machine.")
    except Exception:
        pass

    not_connected = []

    if _has_env("GOOGLE_MAPS_API_KEY") or _has_env("TOMTOM_API_KEY"):
        pass   # routes.eta is in the tool menu
    else:
        not_connected.append("Travel times / traffic (needs GOOGLE_MAPS_API_KEY with the Routes API enabled, or TOMTOM_API_KEY)")
    tiers_now = config.load().get("model_tiers", {})
    vision_ok = tiers_now.get("frontier", {}).get("provider") == "openai"
    if vision_ok:
        lines.append("- See and analyze PHOTOS sent in the chat — describe, read text, answer about them (photo turns are their own turn; actions need a text message).")
        try:
            from kyraan.agents import faces as _faces
            if _faces.available():
                lines.append('- Recognize ENROLLED faces in photos (on-machine only). Remember-a-face asks -> faces.remember, never a resend-with-caption instruction. Delete: "forget the face <name>".')
        except Exception:
            pass
    lines.append('- A shared location pin arrives as "[I\'m sharing my current location: <place> (lat, lon)]" — use it for local answers immediately; never ask which city after a pin. You cannot request or track location.')
    lines.append("- The owner's phone reports its position to Home Assistant (2026-09-04): "
                 "\"where am I\" is answered deterministically from that — never from a saved "
                 "address, never by asking for a pin.")
    from kyraan.agents.commands import brief_line
    lines.append("- " + brief_line())
    lines.append("- The OWNER can grant or revoke another person's chat access right here: persons.set_access (\"enroll ruma\", \"cut X off\"). Granting requires their recorded consent + chat id and a clean subject review; revoking is instant. persons.add only makes someone trackable — it gives them nothing.")

    if _has_env("GOOGLE_CALENDAR_ICS_URL"):
        pass   # calendar.list_events is in the tool menu
    else:
        not_connected.append("Calendar reading (needs the calendar's secret ICS URL)")
    if _has_env("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"):
        if os.environ.get("KYRAAN_EMAIL_BODIES", "").strip() == "local":
            lines.append('- Email bodies are read by the local model on this computer and never leave the machine (email.read).')
        else:
            lines.append('- Check unread email: senders and subjects ONLY, never message bodies (a deliberate privacy boundary — say so if asked to open/summarize an email, and point to Gmail).')
    else:
        not_connected.append("Calendar event creation and email checking (needs the Google OAuth setup)")

    if _has_env("SEARXNG_URL"):
        pass   # web.search is in the tool menu
    else:
        not_connected.append("Web search (needs the local SearXNG container — SEARXNG_URL in .env)")

    if _has_env("HASS_URL", "HASS_TOKEN"):
        server = (config.load().get("tool_servers") or {}).get("home_assistant") or {}
        sensors = [e for e in (server.get("read_entities") or []) if e.startswith("sensor.")]
        switches = server.get("write_entities") or []
        if switches:
            names = ", ".join(_friendly_entity(e) for e in switches)
            lines.append(f"- Switchable home devices: {names}. Readable: temperature/humidity, plug power and energy, purifier air quality.")
    else:
        not_connected.append("Smart home (needs the Home Assistant URL + token)")

    from kyraan.triggers.briefs import brief_time  # lazy: avoids import order issues
    at = brief_time()
    if at is not None:
        lines.append(f"- A morning brief arrives daily at {at.strftime('%H:%M')} (calendar, reminders, home status).")
    lines.append("")
    if _has_env("SEARXNG_URL"):
        # The tool exists — but the honesty rule survives it: internet
        # access ends at search snippets, and saying otherwise is the same
        # hallucination the original hard "no internet" block prevented.
        lines.append(
            "INTERNET ACCESS IS EXACTLY the web.search tool: result titles and "
            "snippets only. You cannot open pages, click links, browse sites, "
            "fetch URLs, or check anything a search snippet doesn't show — and "
            "any live claim not backed by a search THIS exchange is your "
            "training data talking; say so or search first."
        )
    else:
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
    email_truth = (
        "email bodies are read only when you ask, processed entirely by the "
        "local model on this computer, and never sent to any cloud service"
        if os.environ.get("KYRAAN_EMAIL_BODIES", "").strip() == "local"
        else "email bodies are never read")
    lines.append(
        "IF ASKED ABOUT DATA OR PRIVACY, answer with exactly these truths: "
        "everything runs on the owner's own computer; facts you're told are "
        f"stored as local files only after the owner reviews them; {model_truth}; "
        "nothing is ever used to train models; "
        f"{email_truth}; nothing is shared with anyone else."
    )

    if not_connected:
        lines.append("")
        lines.append("SET UP BUT NOT CONNECTED YET (say what's missing, in one line, if asked):")
        lines.extend(f"- {item}" for item in not_connected)

    lines.append("")
    try:
        from kyraan.channels import voice as _voice2
        voice_cannot = "" if _voice2.available() else "voice notes, "
    except Exception:
        voice_cannot = "voice notes, "
    browsing_cannot = ("opening full web pages (search snippets are the limit), "
                       if _has_env("SEARXNG_URL") else "web browsing, ")
    email_cannot = ("" if os.environ.get("KYRAAN_EMAIL_BODIES", "").strip() == "local"
                    else "opening email bodies, ")
    images_cannot = (
        "GENERATING IMAGES (you can SEE photos sent in chat, but cannot "
        "create, draft, or edit any image)"
        if vision_ok else
        "GENERATING OR VIEWING IMAGES (you cannot create, draft, see, or "
        "analyze any image or photo — do not offer to)")
    lines.append(
        f"EVERYTHING ELSE — {browsing_cannot}bookings, calls, music, payments, "
        f"{email_cannot}editing/rescheduling calendar events, "
        f"{images_cannot}, {voice_cannot}devices not "
        "listed above — you can NOT do yet. When asked, say so plainly in one "
        "short line, like a capable human assistant would (\"I can't book cabs "
        "yet\") — no apology spiral, no inventing abilities, and offer an "
        "alternative only when a listed capability genuinely helps (for an "
        "image request: you can write a detailed prompt for an image tool, "
        "but say clearly the image itself must be made elsewhere)."
    )
    return "\n".join(lines)
