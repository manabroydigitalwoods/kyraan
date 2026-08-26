"""Legacy classifier-path skill handlers — the degraded-mode fallback.

Split out of orchestrator.py (2026-08-27) at 1,400+ lines: since the
agent loop became the primary brain, these handlers run only when BOTH
loop tiers fail and the classifier routes the message. They are proven
code kept intact, not a place new features land — new capabilities go to
the tool registry + loop_tools.py.

Each handler references live orchestrator state (confirm stash, history,
contextvars) through the module object (`orch.`) so test monkeypatches
on orchestrator keep applying.
"""
import json
import time

from kyraan.agents.guards import _is_greeting
from kyraan.agents.prompts import (
    _ANSWER_SYSTEM, _EXTRACT_EVENT_SYSTEM, _EXTRACT_WHEN_SYSTEM,
    _EXTRACT_WINDOW_SYSTEM,
)
from kyraan.control_plane import kernel
from kyraan.memory import store as memory_store
from kyraan.control_plane.dnd import humanize, local_now
from kyraan.control_plane.kernel import ConfirmationRequired, SkillCall
from kyraan.control_plane.logging_setup import log_event
from kyraan.model_router import router
from kyraan.triggers import scheduler


class _OrchestratorProxy:
    """Call-time access to orchestrator state — import-order-proof (a
    module-level import here is circular with orchestrator's bottom
    import of these handlers), and monkeypatches on orchestrator
    attributes are seen because every access goes through getattr."""

    def __getattr__(self, name):
        from kyraan.agents import orchestrator
        return getattr(orchestrator, name)


orch = _OrchestratorProxy()

async def _create_reminder(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        # cheap, backed by llama3.1:8b as of 2026-08-25 — the earlier 3B
        # model (llama3.2) produced malformed JSON here and once embedded
        # prose inside the when_iso value itself, corrupting the datetime
        # outright, so every call was moved to frontier. llama3.1:8b tested
        # clean and correct across every sample (4/4, matching frontier
        # exactly) — see config/permissions.yaml's model_tiers comment.
        # No max_tokens cap below the router's default: a reasoning-model
        # tier (frontier, or if this ever points at one again) spends
        # hidden tokens before the visible JSON, and a 200-token cap
        # truncated the output mid-string live (2026-08-25).
        extracted = await orch._structured_call(text, _EXTRACT_WHEN_SYSTEM.format(now=local_now().isoformat()))
        try:
            data = json.loads(router.strip_code_fence(extracted.text))
            if orch.is_time_fragment(str(data.get("text", ""))):
                # A reminder whose TEXT is itself just a time phrase is a
                # broken extraction ("tomorrow morning" at 6 AM, seen live).
                return "Remind you about what? Tell me the task and I'll set it."
            data["when_iso"] = orch._anchor_clock_time(text, data["when_iso"])
            existing = scheduler.find_duplicate(chat_id, data["text"], data["when_iso"])
            if existing:
                return (
                    f"Already set: \"{existing.text}\" at {humanize(existing.when_iso)} "
                    f"(id {existing.id[:8]}) — I didn't add a duplicate."
                )
            reminder = scheduler.create_reminder(chat_id, data["text"], data["when_iso"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            # Kept as a safety net even though frontier is far more
            # reliable — no model is perfect, and this must never crash.
            log_event("reminder_extraction_failed", text=text, raw=extracted.text, error=str(exc))
            return "I couldn't work out a time for that reminder — try rephrasing with a clearer date/time."
        return f"Reminder set: \"{data['text']}\" at {humanize(data['when_iso'])} (id {reminder.id[:8]})"

    return await orch._gated(chat_id, SkillCall("reminders.create", {"text": text}), handler)


async def _list_reminders(chat_id: int) -> str:
    async def handler(_args: dict) -> str:
        pending = scheduler.store.list_pending(chat_id)
        if not pending:
            return "No pending reminders."
        return "\n".join(f"- [{r.id[:8]}] {r.text} at {humanize(r.when_iso)}" for r in pending)

    return await orch._gated(chat_id, SkillCall("reminders.list", {}), handler)


async def _cancel_reminder(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        pending = scheduler.store.list_pending(chat_id)
        if not pending:
            return "No matching reminder found to cancel."
        # Match any token in the message against pending ids — the old
        # single-token extraction grabbed the *first* >=6-char word, which
        # was usually "cancel" itself, so a real id later in the message
        # never matched. Ordinary words can't collide with a uuid-hex
        # prefix ("cancel" contains non-hex letters), so checking every
        # token is safe.
        tokens = [t.lower() for t in args["text"].split() if len(t) >= 6 and t.isalnum()]
        match = next((r for r in pending if any(r.id.startswith(t) for t in tokens)), None)
        if not match:
            # Match by description too ("cancel the call mom one") — the
            # context-aware classifier rewrites follow-ups into phrases
            # like this, and demanding an id for them dead-ends the
            # conversation. Only an UNAMBIGUOUS description match cancels;
            # words hitting several reminders still ask.
            stop = {"cancel", "the", "reminder", "reminders", "one", "that",
                    "this", "delete", "remove", "please", "for", "about", "set"}
            words = [w.lower().strip(".,!?'\"") for w in args["text"].split()]
            words = [w for w in words if len(w) >= 3 and w not in stop]
            candidates = [r for r in pending if any(w in r.text.lower() for w in words)] if words else []
            if len(candidates) == 1:
                match = candidates[0]
        if not match and len(pending) == 1:
            # Only one reminder exists — "cancel my reminder" is unambiguous.
            match = pending[0]
        if not match:
            # Several pending and no id in the message: cancelling a guess
            # is destructive and silent when it's wrong (a live walkthrough
            # only passed here because the intended reminder happened to be
            # first in the list). Ask instead.
            listing = "\n".join(f"- [{r.id[:8]}] {r.text} at {humanize(r.when_iso)}" for r in pending)
            return (
                "You have more than one pending reminder — which should I cancel? "
                f"Reply like \"cancel {pending[0].id[:8]}\":\n{listing}"
            )
        scheduler.cancel_reminder(match.id)
        return f"Cancelled reminder: \"{match.text}\""

    return await orch._gated(chat_id, SkillCall("reminders.cancel", {"text": text}), handler)


async def _list_calendar(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        window = await orch._structured_call(args["text"], _EXTRACT_WINDOW_SYSTEM.format(now=local_now().isoformat()))
        try:
            data = json.loads(router.strip_code_fence(window.text))
            start, end = data["start_iso"], data["end_iso"]
            label = data.get("label") or "that period"
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log_event("calendar_window_extraction_failed", text=args["text"], raw=window.text, error=str(exc))
            return "I couldn't work out which period you mean — try e.g. \"what's on my calendar tomorrow?\""

        try:
            events = await kernel.run_tool(kernel.ToolCall("calendar.list_events", {"start": start, "end": end}))
        except kernel.ToolFailed as exc:
            # on_failure: surface — the message is written to be shown.
            return f"Couldn't check the calendar: {exc}"

        if not events:
            return f"Nothing on the calendar {label}."
        lines = []
        for e in events:
            when = "all day" if e["all_day"] else humanize(e["start"])
            where = f" ({e['location']})" if e.get("location") else ""
            lines.append(f"- {when} — {e['title']}{where}")
        return f"Calendar {label}:\n" + "\n".join(lines)

    return await orch._gated(chat_id, SkillCall("calendar.list", {"text": text}), handler)


async def _cancel_event(chat_id: int, text: str) -> str:
    """Cancel calendar events. Targets are resolved BEFORE the confirm
    gate so the ask names exactly what will be removed — born from a live
    disaster: with no cancel capability, qa PROMISED cancellation twice
    and the classifier then created a junk event titled 'Cancel All
    Events' on the real calendar."""
    from datetime import timedelta

    window = await orch._structured_call(text, _EXTRACT_WINDOW_SYSTEM.format(now=local_now().isoformat()))
    try:
        data = json.loads(router.strip_code_fence(window.text))
        start, end = data["start_iso"], data["end_iso"]
        label = data.get("label") or "that period"
    except (json.JSONDecodeError, KeyError, TypeError):
        # No time phrase ("cancel the test event") is normal — search the
        # coming week.
        start = local_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end = (local_now() + timedelta(days=7)).isoformat()
        label = "the next 7 days"

    try:
        events = await kernel.run_tool(kernel.ToolCall("calendar.list_events", {"start": start, "end": end}))
    except kernel.ToolFailed as exc:
        return f"Couldn't check the calendar: {exc}"
    events = [e for e in events if e.get("id")]
    if not events:
        return f"Nothing on the calendar {label} to cancel."

    stop = {"cancel", "cancle", "delete", "remove", "the", "a", "an", "event",
            "events", "all", "my", "from", "calendar", "please", "meeting",
            "meetings", "appointment", "today", "tomorrow", "this", "that",
            "week", "weekend", "month", "months", "year", "next", "last",
            "coming", "morning", "evening", "afternoon", "tonight",
            "yes", "right", "now", "it", "them", "and", "of", "for",
            "can", "you", "everything", "every",
            "to", "through", "thru", "during", "until", "till", "between",
            "on", "in", "at", "after", "before", "starting", "ending"}
    words = {w.strip(".,!?\"'").lower() for w in text.split()}
    # Time vocabulary can never be a title filter (round-8: subtracting
    # the extractor's label words broke when the label was humanized —
    # user "feb", label "February 2099"; the vocabulary check works on
    # the user's own tokens and needs no agreement between the two).
    from kyraan.agents.guards import is_window_word
    content = {w for w in words - stop if w and not is_window_word(w)}
    if content:
        targets = [e for e in events
                   if content & {w.strip(".,!?\"'").lower() for w in e["title"].split()}]
        if not targets:
            listing = "\n".join(f"- {humanize(e['start'])} — {e['title']}" for e in events[:8])
            return (f"I couldn't match that to an event {label}. On the calendar:\n"
                    f"{listing}\nWhich one should I cancel?")
    elif words & {"all", "everything", "every"}:
        targets = list(events)  # explicitly asked for everything in the window
    else:
        # Bare "can you cancel" with no object — a human asks which, never
        # defaults to sweeping the whole calendar (live 2026-08-26: it
        # escalated straight to a DELETE-4-events ask).
        listing = "\n".join(f"- {humanize(e['start'])} — {e['title']}" for e in events[:8])
        return (f"Cancel which event? On the calendar {label}:\n{listing}\n"
                "Name the one to cancel — or say \"cancel all events\" for all of them.")

    # Recurring occurrences share their series id — deleting it removes
    # the whole series, so collapse duplicates and say it out loud.
    seen, unique = set(), []
    for e in targets:
        if e["id"] not in seen:
            seen.add(e["id"])
            unique.append(e)
    overflow = 0
    if len(unique) > 8:
        # Never confirm more than the kernel's 8-call rail can actually
        # run (review P2: the ask covered the full batch, execution
        # stopped at eight). The remainder is named up front.
        overflow = len(unique) - 8
        unique = unique[:8]

    async def handler(args: dict) -> str:
        # Per-event isolation + a COMPLETE receipt: deleted /
        # outcome-unknown / untouched are three DIFFERENT truths — a
        # timed-out delete may have succeeded, and calling it "not
        # touched" invites a double-delete (round-5 P2).
        deleted, already_gone, untouched = [], [], []
        unknown = ""
        stop_reason = ""
        for i, e in enumerate(unique):
            try:
                result = await kernel.run_tool(kernel.ToolCall(
                    "calendar.delete_event", {"event_id": e["id"], "title": e["title"]}))
            except kernel.ToolFailed as exc:
                stop_reason = str(exc)
                if "MAY still have gone" in stop_reason:
                    unknown = e["title"]
                    untouched = [x["title"] for x in unique[i + 1:]]
                else:
                    untouched = [x["title"] for x in unique[i:]]
                break
            (already_gone if result.get("already_gone") else deleted).append(e["title"])
        parts = []
        if deleted:
            parts.append("Deleted from your calendar: " + ", ".join(f'"{t}"' for t in deleted))
        if already_gone:
            parts.append("Already gone: " + ", ".join(f'"{t}"' for t in already_gone))
        if unknown:
            parts.append(f'Outcome UNKNOWN for "{unknown}" — the delete timed out and may '
                         "have succeeded; check the calendar before retrying it")
        remaining = len(untouched) + overflow
        # The resume phrase reconstructs the EFFECTIVE filter (round-8:
        # 'cancel all yoga events' resumed as 'cancel all events' and
        # would sweep unrelated events into the next confirm ask). Built
        # from `content`, it round-trips through this matcher by
        # construction.
        title_part = (" ".join(sorted(content)) + " ") if content else ""
        resume = f'say "cancel all {title_part}events {label}" again'
        if untouched:
            parts.append(f"NOT touched ({stop_reason.split(':')[0]}): "
                         + ", ".join(f'"{t}"' for t in untouched))
        if remaining:
            # ALWAYS account for everything beyond what ran (round-6 P2:
            # overflow silently vanished whenever a batch stopped early),
            # and the resume phrase carries the ORIGINAL WINDOW so
            # "next month" doesn't resume as "today". Fresh listing on
            # the re-run stays the design: cached ids go stale, and
            # already-deleted events resolve harmlessly.
            parts.append(f"{remaining} event(s) still to cancel — {resume}")
        return ". ".join(parts) if parts else "Nothing was deleted."

    described = "\n".join(
        f"- {humanize(e['start'])} — {e['title']}"
        + (" (recurring — the WHOLE series will be removed)" if e.get("recurring") else "")
        for e in unique)
    describe = (f"About to DELETE {len(unique)} event(s) from your Google Calendar:\n"
                f"{described}\nThis can't be undone from here")
    if overflow:
        title_part = (" ".join(sorted(content)) + " ") if content else ""
        describe += (f"\n({overflow} more matched — this batch is capped at 8; "
                     f'say "cancel all {title_part}events {label}" again afterwards for the rest)')
    return await orch._gated(chat_id, SkillCall("calendar.cancel", {"text": text}), handler, describe=describe)


async def _create_event(chat_id: int, text: str) -> str:
    # Extraction runs BEFORE the confirm gate, and the parsed fields go
    # into the stashed SkillCall args — so what the user confirms is
    # byte-identical to what runs. Re-extracting on "yes" could produce a
    # different time than the one shown (model nondeterminism).
    extracted = await orch._structured_call(text, _EXTRACT_EVENT_SYSTEM.format(now=local_now().isoformat()))
    def clean_iso(value: str) -> str:
        # _parse_when gives the same protections events as reminders get
        # (naive -> local tz, model's spurious Z -> local wall time), and
        # microsecond junk from the model (seen live: 15:00:00.000123) is
        # noise, never intent.
        return scheduler._parse_when(str(value)).replace(microsecond=0).isoformat()

    try:
        data = json.loads(router.strip_code_fence(extracted.text))
        # ONE time normalization shared with the agent loop (round-6 P2:
        # this path had drifted behind — no tolerance on the anchor, no
        # end>start check): guards.normalized_event_times does sanitize,
        # tolerant anchoring against the user's words, and range sanity.
        from kyraan.agents.guards import normalized_event_times
        start_iso, end_iso = normalized_event_times(
            {"start": str(data["start_iso"]), "end": str(data["end_iso"])}, text)
        args = {
            "title": str(data["title"]),
            "start": clean_iso(start_iso),
            "end": clean_iso(end_iso),
        }
        location = data.get("location")
        # Models sometimes emit the STRING "null" instead of JSON null —
        # seen live as an event 'at null'.
        if location and str(location).strip().lower() not in ("null", "none"):
            args["location"] = str(location)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        log_event("event_extraction_failed", text=text, raw=extracted.text, error=str(exc))
        return "I couldn't work out the event details — try e.g. \"add a meeting with Rohan tomorrow 5pm to my calendar\"."

    # A fabricated or mistyped PAST event dies before the confirm ask —
    # walkthrough v3 (degraded mode): "book a flight to delhi" misrouted
    # into an ask for "Delhi Trip, Jan 2024".
    from datetime import timedelta as _td
    if scheduler._parse_when(args["start"]) < local_now() - _td(minutes=5):
        return (
            f"That start time ({humanize(args['start'])}) is in the past — "
            "tell me a future time for the event."
        )

    async def handler(handler_args: dict) -> str:
        try:
            created = await kernel.run_tool(kernel.ToolCall("calendar.create_event", handler_args))
        except kernel.ToolFailed as exc:
            return f"Couldn't create the event: {exc}"
        link = f"\n{created['link']}" if created.get("link") else ""
        return f"Event created on your calendar: \"{created['title']}\" at {humanize(handler_args['start'])}{link}"

    where = f" at {args['location']}" if args.get("location") else ""
    start_h = humanize(args["start"])
    same_day = scheduler._parse_when(args["start"]).date() == scheduler._parse_when(args["end"]).date()
    end_dt = scheduler._parse_when(args["end"])
    end_h = end_dt.strftime("%I:%M %p").lstrip("0") if same_day else humanize(args["end"])
    describe = f"About to create a calendar event: \"{args['title']}\" {start_h} → {end_h}{where}"
    return await orch._gated(chat_id, SkillCall("calendar.create", args), handler, describe=describe)


# v1 home scope: the bedroom AC plug (owner's decision). More devices =
# more entries here + the allowlist in permissions.yaml, nothing else.
_AC_SWITCH = "switch.ac"
_AC_POWER = "sensor.ac_current_consumption"
_AC_TODAY = "sensor.ac_today_s_consumption"
_TEMP = "sensor.bed_room_temp_temperature"
_HUMIDITY = "sensor.bed_room_temp_humidity"


def _since(last_changed: str | None) -> str:
    """'for 2h 05m' from HA's last_changed — '' when unknown."""
    if not last_changed:
        return ""
    try:
        from datetime import datetime

        delta = local_now() - datetime.fromisoformat(last_changed)
    except ValueError:
        return ""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return " for under a minute"
    if minutes < 60:
        return f" for {minutes}m"
    return f" for {minutes // 60}h {minutes % 60:02d}m"


# Rooms Kyraan knows it has NO sensor in — an honest "no sensor there"
# beats answering a kitchen question with bedroom data (seen live).
_UNSENSORED_ROOMS = ("living", "kitchen", "hall", "bathroom", "balcony", "dining", "office")


async def _check_email(chat_id: int, text: str = "") -> str:
    # "Open"/"read the email" asks for the body — which Kyraan deliberately
    # never fetches (§3a: metadata only). Say the boundary instead of
    # dumping the same list again (seen live: "can you open email?" got an
    # identical unread summary, as if it answered the question).
    from kyraan.agents.guards import wants_email_body
    wants_body = wants_email_body(text)

    async def handler(_args: dict) -> str:
        # The reply the user sees carries senders/subjects; when any model
        # tier is a CLOUD provider the history records only a placeholder,
        # so none of it reaches third parties. With local-only tiers
        # (2026-08-26) redaction is pure capability loss — qa couldn't see
        # the listing the user's follow-up ("are these latest emails?")
        # was asking about — so the real text stays in history.
        if orch._cloud_tier_in_use():
            orch._history_redaction.set("[showed the unread email summary]")
        try:
            result = await kernel.run_tool(kernel.ToolCall("email.unread", {"limit": 5}))
        except kernel.ToolFailed as exc:
            return f"Couldn't check email: {exc}"
        total = result.get("unread_estimate", 0)
        messages = result.get("messages", [])
        if not messages:
            return "No unread emails."
        lines = []
        if wants_body:
            lines.append(
                "I can't open email contents — by design I only see senders and "
                "subjects, never bodies (your data boundary). Open Gmail for the "
                "full message. Latest unread:"
            )
        else:
            lines.append(f"You have about {total} unread. Latest:")
        for m in messages:
            sender = m["from"].split("<")[0].strip().strip('"') or m["from"]
            lines.append(f"- {sender}: {m['subject']}")
        return "\n".join(lines)

    return await orch._gated(chat_id, SkillCall("email.check", {}), handler)


async def _home_query(chat_id: int, text: str) -> str:
    # Deterministic sub-routing on the classifier's cleaned text — device
    # answers stay template-composed, no model between the sensor and the
    # user. The question decides which card(s) to show.
    t = text.lower()
    wants_climate = any(w in t for w in ("temp", "humid", "hot", "warm", "cold", "climate"))
    wants_ac = "ac" in t.split() or any(w in t for w in ("power", "consum", "electric", "running", "watt", "plug"))
    other_room = next((r for r in _UNSENSORED_ROOMS if r in t), None)
    if not wants_climate and not wants_ac:
        wants_climate = wants_ac = True  # generic "home status" — show both

    async def handler(_args: dict) -> str:
        lines = []
        if wants_ac:
            try:
                state = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": _AC_SWITCH}))
                since = _since(state.get("last_changed"))
                if state["state"] != "on":
                    lines.append(f"The AC is OFF{since}.")
                else:
                    detail = ""
                    try:
                        power = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": _AC_POWER}))
                        today = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": _AC_TODAY}))
                        detail = f" — drawing {power['state']} {power['unit'] or 'W'}, {today['state']} {today['unit'] or 'kWh'} today"
                    except kernel.ToolFailed:
                        pass  # the on/off answer stands even if the sensors hiccup
                    lines.append(f"The AC is ON{since}{detail}.")
            except kernel.ToolFailed as exc:
                lines.append(f"Couldn't check the AC: {exc}")
        if wants_climate:
            prefix = ""
            if other_room:
                prefix = f"There's no sensor in the {other_room} room yet — the only climate sensor is in the bedroom. "
            try:
                temp = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": _TEMP}))
                humidity = await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": _HUMIDITY}))
                lines.append(
                    f"{prefix}Bedroom: {temp['state']}{temp['unit'] or '°C'} / "
                    f"{humidity['state']}{humidity['unit'] or '%'} humidity."
                )
            except kernel.ToolFailed as exc:
                lines.append(f"{prefix}Couldn't read the bedroom sensor: {exc}")
        return "\n".join(lines)

    return await orch._gated(chat_id, SkillCall("home.query", {}), handler)


async def _home_control(chat_id: int, text: str) -> str:
    # A control intent without any device mention is a misroute — seen
    # live: "let me fix you" became "Should the AC go on or off?". No
    # device word, no switch talk: answer conversationally instead.
    device_words = {"ac", "plug", "switch", "socket", "appliance"}
    if not (device_words & {w.strip(".,!?") for w in text.lower().split()}):
        return await _answer(chat_id, text)
    # Direction is decided deterministically from the normalized text —
    # a physical switch must never flip on a model's guess. "off" checked
    # first: "turn off" contains no "on", but "on" appears inside many
    # words, so an explicit standalone-word match is used for both.
    words = text.lower().replace(",", " ").split()
    if "off" in words:
        tool, verb = "home.turn_off", "OFF"
    elif "on" in words:
        tool, verb = "home.turn_on", "ON"
    else:
        return "Should the AC go on or off? Say e.g. \"turn off the AC\"."

    async def handler(args: dict) -> str:
        try:
            result = await kernel.run_tool(kernel.ToolCall(tool, args))
        except kernel.ToolFailed as exc:
            return f"Couldn't switch the AC: {exc}"
        # Read-back truth, not assumption: report what the plug says now —
        # and when HA's state hasn't converged (adapter polled and gave
        # up), say so honestly instead of reporting the stale value as
        # fact (seen live: confirmed ON, reply said OFF).
        if result.get("converged") is False:
            return (
                f"I sent the {verb} command, but the plug still reports "
                f"{result['state'].upper()} — give it a few seconds, then ask \"is the AC on?\" to verify."
            )
        return f"Done — the AC is now {result['state'].upper()}."

    describe = f"About to turn the AC {verb}"
    return await orch._gated(chat_id, SkillCall("home.control", {"entity": _AC_SWITCH}), handler, describe=describe)


async def _answer(chat_id: int, text: str) -> str:
    async def handler(args: dict) -> str:
        # cheap, backed by llama3.1:8b as of 2026-08-25 — asked "what time
        # is it?" with the correct current time given directly in the
        # system prompt, the earlier 3B model (llama3.2) answered wrong in
        # 3/3 tries (14:40, 17:30, 15:50 — actual was 13:50); llama3.1:8b
        # was exactly right in 3/3, matching frontier. See
        # config/permissions.yaml's model_tiers comment.
        # Tier comes from config — moved to frontier 2026-08-25 evening:
        # chat.jsonl showed the local 8B collapsing on multi-turn
        # continuations ("make 2 paragraphs" -> garbled time-talk) and
        # contradicting the capability brief ("Yes, I have internet
        # access") — instruction-following over a now-large system prompt
        # is exactly where it's weakest (Ollama's default context also
        # truncates big prompts silently).
        tier = kernel.config.skill_config("qa.answer")["model_tier"]
        from kyraan.memory import engine
        system = _ANSWER_SYSTEM.format(
            now=local_now().isoformat(),
            capabilities=orch.capability_brief(),
            facts=engine.memory_context(args["text"]),
            pending_facts=memory_store.load_pending_facts() or "(none)",
            history=orch._history_block(chat_id),
        )
        try:
            response = await router.acall(prompt=args["text"], system=system, tier=tier)
        except router.ModelProviderError as exc:
            # Same degradation as intent classification: a frontier outage
            # (seen live: Groq's free 200k-token/day cap exhausted) drops
            # to the local model instead of failing the conversation.
            if tier == "cheap":
                raise
            log_event("qa_fallback_cheap", error=str(exc))
            # Degraded-mode self-awareness — live transcript: the user said
            # "you are confused / randomly answering" while the fallback
            # model spiraled, and Kyraan never admitted its state.
            system += (
                "\n\nIMPORTANT: you are currently running on the smaller "
                "LOCAL backup model because the main model is rate-limited. "
                "Keep replies short and factual. If the user says you seem "
                "confused, wrong, or repetitive, tell them honestly: the main "
                "model is temporarily rate-limited and reply quality is "
                "reduced for a few minutes — don't argue or deflect."
            )
            response = await router.acall(prompt=args["text"], system=system, tier="cheap")
            tier = "cheap"
        reply = response.text
        recent = [t.strip() for role, t in list(orch._history[chat_id])[-6:] if role == "assistant"]
        # A pathological loop repeats within MINUTES to different
        # questions; greeting a greeting identically hours later is just
        # being human. Found live: after history seeding, "helo" the next
        # morning matched last night's greeting reply and got the
        # I'm-repeating-myself apology. The guard needs a live exchange
        # this process (< 15 min) and never fires on a greeting.
        recently_active = time.monotonic() - orch._last_reply_at.get(chat_id, float("-inf")) < 900
        if (reply.strip() and reply.strip() in recent and recently_active
                and not _is_greeting(args["text"])):
            # A human never sends the same sentence twice in a row —
            # verbatim repetition is a small-model failure mode (seen
            # live 2026-08-26: "I can't book cabs yet." to three
            # different questions). One retry with the problem named;
            # if it STILL repeats, admit it instead of looping.
            log_event("qa_repetition_detected", chat_id=chat_id, reply=reply[:80])
            retry = await router.acall(prompt=args["text"], system=system + (
                "\n\nIMPORTANT: your previous draft repeated one of your own "
                "earlier replies word-for-word. Answer THIS message "
                "specifically; do not reuse any earlier sentence."), tier=tier)
            if retry.text.strip() and retry.text.strip() not in recent:
                return retry.text
            return ("I'm repeating myself — sorry, I didn't process that "
                    "properly. Could you say it another way?")
        return reply

    return await orch._gated(chat_id, SkillCall("qa.answer", {"text": text}), handler)
