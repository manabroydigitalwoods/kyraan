"""The model-driven tool loop — Kyraan's primary brain since 2026-08-26.

One frontier model sees the conversation, the owner's saved memory, and a
menu of callable tools, then decides: call a tool (and see its result), or
reply. This replaces classify-and-dispatch as the first path because the
classifier architecture kept failing on questions no rule anticipated
("are these latest emails?", "show me kiaan memories", "can you cancel") —
each needed a hand-written rule; a reader with tools needs none.

Safety is layered, not replaced:
- Every tool still runs through kernel.run_tool: kill switch, permission
  gates, loop rails, audit log. A confirm-gated write raises
  ConfirmationRequired here exactly as it does everywhere — the loop turns
  it into the standard ask, and the owner's yes runs the EXACT stashed
  call, byte-identical.
- The loop runs on the FRONTIER tier only. Any provider failure or
  unparseable decision raises AgentUnavailable and the orchestrator falls
  back to the proven classifier path — degraded mode is unchanged.
- Deterministic guards (time-fragment patience, confirm words, review
  decisions) run BEFORE the loop in the orchestrator, as always.
"""
import json

from kyraan.agents.capabilities import capability_brief
from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import humanize, local_now
from kyraan.control_plane.logging_setup import log_event
from kyraan.memory import store as memory_store
from kyraan.model_router import router
from kyraan.triggers import scheduler

_MAX_STEPS = 5  # decision calls per message; kernel's own rails cap tool runs


class AgentUnavailable(Exception):
    """The loop can't run (provider down, or the model can't produce a
    usable decision) — the caller falls back to the classifier path."""


# --- tool executors -------------------------------------------------------
# Each returns a JSON-serializable result for the model to read, or raises
# kernel.ConfirmationRequired (writes) / kernel.ToolFailed (surfaced).

async def _calendar_list(chat_id: int, args: dict, raw_text: str):
    events = await kernel.run_tool(kernel.ToolCall(
        "calendar.list_events", {"start": args["start"], "end": args["end"]}))
    return events[:20]


async def _calendar_create(chat_id: int, args: dict, raw_text: str):
    start = scheduler._sanitize_iso(str(args["start"]))
    end = scheduler._sanitize_iso(str(args["end"]))
    if scheduler._parse_when(start) < local_now():
        raise kernel.ToolFailed("that start time is in the past — ask the user for the intended date")
    call_args = {"title": args["title"], "start": start, "end": end}
    if args.get("location"):
        call_args["location"] = args["location"]
    return await kernel.run_tool(kernel.ToolCall("calendar.create_event", call_args))


async def _calendar_delete(chat_id: int, args: dict, raw_text: str):
    return await kernel.run_tool(kernel.ToolCall(
        "calendar.delete_event",
        {"event_id": args["event_id"], "title": args.get("title", "")}))


async def _email_unread(chat_id: int, args: dict, raw_text: str):
    return await kernel.run_tool(kernel.ToolCall(
        "email.unread", {"limit": min(int(args.get("limit", 5)), 10)}))


async def _home_get_state(chat_id: int, args: dict, raw_text: str):
    return await kernel.run_tool(kernel.ToolCall("home.get_state", {"entity": args["entity"]}))


async def _reminders_create(chat_id: int, args: dict, raw_text: str):
    when_iso = scheduler._sanitize_iso(str(args["when_iso"]))
    scheduler._parse_when(when_iso)  # validate before anything persists
    from kyraan.agents import orchestrator
    when_iso = orchestrator._anchor_clock_time(raw_text, when_iso)
    if orchestrator.is_time_fragment(str(args["text"])):
        raise kernel.ToolFailed("the reminder text is just a time phrase — ask the user what the reminder is FOR")
    existing = scheduler.find_duplicate(chat_id, args["text"], when_iso)
    if existing:
        return {"duplicate": True, "id": existing.id[:8], "text": existing.text,
                "when": humanize(existing.when_iso)}
    reminder = scheduler.create_reminder(chat_id, args["text"], when_iso)
    return {"created": True, "id": reminder.id[:8], "text": args["text"],
            "when": humanize(when_iso)}


async def _reminders_list(chat_id: int, args: dict, raw_text: str):
    return [{"id": r.id[:8], "text": r.text, "when": humanize(r.when_iso)}
            for r in scheduler.store.list_pending(chat_id)]


async def _reminders_cancel(chat_id: int, args: dict, raw_text: str):
    wanted = str(args["reminder_id"]).lower()
    match = next((r for r in scheduler.store.list_pending(chat_id)
                  if r.id.startswith(wanted)), None)
    if match is None:
        raise kernel.ToolFailed(f"no pending reminder with id {wanted!r} — list reminders first")
    scheduler.cancel_reminder(match.id)
    return {"cancelled": True, "text": match.text}


async def _usage_report(chat_id: int, args: dict, raw_text: str):
    from kyraan.model_router import usage_report
    # Robust coercion — the model was seen sending days="few days" after a
    # too-clever param description. Any unparseable value means the default.
    raw_days = args.get("days", 7)
    try:
        days = int(float(raw_days))
    except (TypeError, ValueError):
        days = 7
    return usage_report.usage_summary(days=days)


async def _memory_forget(chat_id: int, args: dict, raw_text: str):
    from kyraan.memory import engine
    wanted = str(args.get("fact", "")).strip()
    if len(wanted) < 3:
        raise kernel.ToolFailed("say which fact to forget, quoting it roughly")
    matches = engine.find_matches(wanted)
    if not matches:
        raise kernel.ToolFailed(
            f"no saved fact matches {wanted!r} — show the user what IS saved and ask which to forget")
    if not kernel.confirmed_context():
        raise kernel.ConfirmationRequired("memory.forget", {"fact": wanted})
    forgotten = engine.forget([m["id"] for m in matches])
    return {"forgotten": forgotten}


async def _memory_pending(chat_id: int, args: dict, raw_text: str):
    from kyraan.agents import orchestrator
    return [{"n": i + 1, "fact": fact, "target": target}
            for i, (_, target, fact) in enumerate(orchestrator._load_review_proposals())]


TOOLS = {
    "calendar.list_events": {
        "params": '{"start": "<ISO datetime>", "end": "<ISO datetime>"}',
        "about": "Events on the Google Calendar in a time window (each has id/title/start/recurring).",
        "run": _calendar_list,
    },
    "calendar.create_event": {
        "params": '{"title": "...", "start": "<ISO>", "end": "<ISO>", "location": "optional"}',
        "about": "Create a calendar event. Asks the owner to confirm first — that is automatic. A start date in the PAST is refused — point it out instead of asking for details. Missing end time: default to one hour, don't ask.",
        "run": _calendar_create,
    },
    "calendar.delete_event": {
        "params": '{"event_id": "<id from calendar.list_events>", "title": "<its title>"}',
        "about": "Delete ONE event by id (list first to get ids). Confirm is automatic. Deleting a recurring event removes its whole series — warn in your ask context.",
        "run": _calendar_delete,
    },
    "email.unread": {
        "params": '{"limit": 5}',
        "about": "Unread email senders and subjects ONLY — bodies are never available, by design.",
        "run": _email_unread,
    },
    "home.get_state": {
        "params": '{"entity": "<e.g. switch.ac, sensor.bed_room_temp_temperature>"}',
        "about": "Read a smart-home entity's state (AC switch, power, energy, bedroom temperature/humidity).",
        "run": _home_get_state,
    },
    "reminders.create": {
        "params": '{"text": "<what to remind>", "when_iso": "<ISO with the user\'s +05:30 offset>"}',
        "about": "Set a reminder delivered as a Telegram message. Only when the user asked to be reminded/woken/alerted.",
        "run": _reminders_create,
    },
    "reminders.list": {
        "params": "{}",
        "about": "The user's pending reminders (id, text, when).",
        "run": _reminders_list,
    },
    "reminders.cancel": {
        "params": '{"reminder_id": "<id prefix from reminders.list>"}',
        "about": "Cancel one pending reminder by id (list first if unsure).",
        "run": _reminders_cancel,
    },
    "usage.report": {
        "params": '{"days": 7}',
        "about": "Kyraan's own AI usage: per-day model calls, input/output/cached tokens, cost in USD, and the live daily budget picture. For 'how much did we spend', 'token usage this week', 'are we near the budget'. days is a NUMBER (vague ranges: use 7) — call directly, never ask which.",
        "run": _usage_report,
    },
    "memory.forget": {
        "params": '{"fact": "<roughly the fact to forget, e.g. \'father Deven Roy\'>"}',
        "about": "Forget a saved fact (deactivates it; kept as history). Matching is deterministic; the owner confirms the exact facts before anything is forgotten. For corrections prefer stating the new fact — supersession handles it.",
        "run": _memory_forget,
    },
    "memory.pending_list": {
        "params": "{}",
        "about": "Facts queued for the owner's review, numbered. To approve/reject, tell the user to say \"review memory\" — you cannot approve.",
        "run": _memory_pending,
    },
}


def _register_home_switches() -> None:
    async def on(chat_id, args, raw_text):
        return await kernel.run_tool(kernel.ToolCall("home.turn_on", {"entity": args["entity"]}))

    async def off(chat_id, args, raw_text):
        return await kernel.run_tool(kernel.ToolCall("home.turn_off", {"entity": args["entity"]}))

    TOOLS["home.turn_on"] = {
        "params": '{"entity": "switch.ac"}',
        "about": "Switch a plug ON. Only when the user asked. Confirm is automatic.",
        "run": on,
    }
    TOOLS["home.turn_off"] = {
        "params": '{"entity": "switch.ac"}',
        "about": "Switch a plug OFF. Only when the user asked. Confirm is automatic.",
        "run": off,
    }


_register_home_switches()


# Token economics: OpenAI bills CACHED input at a ~90% discount, and
# caching is automatic for a byte-stable prompt prefix (>=1024 tokens).
# Everything in this system prompt is therefore STATIC — identity, tools,
# doctrine — so it caches across every call all day; everything that
# changes (time, facts, history, the message) rides in the prompt half,
# AFTER the stable prefix. Nothing is trimmed — only ordered for the
# discount. Do not move dynamic values in here.
_AGENT_SYSTEM = """You are Kyraan, Manab's personal assistant, deciding how to
handle his latest message. The CONTEXT block in the request carries the
current date/time (the user's own timezone — a stated clock time is always
wall-clock in this zone), the owner-reviewed facts, and the conversation.

{capabilities}

TOOLS you can call (results come back to you before you answer):
{tools}

Before EVERY decision, walk the owner's six questions — this is the
doctrine, in order:
1. WANT — what is the user actually after? Read the whole conversation,
   not just the last message; a fragment continues the thought before it.
2. HAVE — which of the tools and known facts cover it?
3. NEED — what's missing? If a required detail only the user knows is
   missing, reply with ONE specific question. Never guess it. But a
   detail with a sensible default ("last few days" -> a tool's default
   window) is NOT missing — use the default instead of asking.
4. CAN — is it within the tools at all? If a listed tool answers the
   question, CALL IT NOW — never tell the user to rephrase or to "say"
   some phrase for something you can do yourself this turn (seen live:
   a spend question got "Say 'report AI spend'" instead of the report).
   If no tool covers it, say so plainly in one line; never invent an
   ability, never promise a workaround you can't do.
5. HOW — the shortest tool chain that does it: list before delete, read
   before summarize. You see each result before deciding again.
6. OKAY FOR THE USER — would the outcome surprise or harm them? Prefer
   the smaller action; anything irreversible or broad ("all events")
   deserves a narrower reading or a check-in first. The system asks the
   owner's yes for every write automatically — NEVER claim an action
   already happened, and never promise future actions ("I'll check"):
   act now or say what to ask for.

DECIDE with ONE JSON object, nothing else:
  {{"action": "reply", "consider": "<one short line: WANT/HAVE/NEED verdict>", "text": "<your reply>"}}
  {{"action": "call", "consider": "<one short line: why this tool now>", "tool": "<tool name>", "args": {{...}}}}

Style rules:
- The USER message may contain several lines sent as a rapid burst — read
  them as ONE thought (greetings fold in; fragments continue each other)
  and answer everything in ONE reply.
- Live data (calendar, email, reminders, home) must come from a tool call
  in THIS exchange — never from memory of earlier listings, never invented.
- Known facts in the CONTEXT are owner-reviewed — treat as true; never
  invent personal facts beyond them and the conversation. Facts listed as
  awaiting review are usable in conversation but not yet permanent.
- Facts tagged [SENSITIVE] or [EMOTIONAL] demand care: bring them up only
  when the user's message is directly about them, always with warmth and
  discretion — never casually, never in a task answer, never as a joke.
  [HEALTH]/[SAFETY]/[EMERGENCY] facts exist to protect the user — weigh
  them whenever health or safety is at stake.
- Reply in the user's tone: brief, warm, direct. No markdown bold.
- If a tool errors, tell the user honestly what failed; don't retry blindly."""


def _tools_block() -> str:
    return "\n".join(f"- {name} {spec['params']}\n    {spec['about']}"
                     for name, spec in TOOLS.items())


def _describe_call(tool: str, args: dict) -> str:
    """The confirm ask the owner sees — concrete, named values."""
    if tool == "calendar.create_event":
        return (f"About to create a calendar event: \"{args.get('title')}\" "
                f"{humanize(str(args.get('start')))} → {humanize(str(args.get('end')))}")
    if tool == "calendar.delete_event":
        return f"About to DELETE \"{args.get('title') or args.get('event_id')}\" from your Google Calendar"
    if tool == "memory.forget":
        from kyraan.memory import engine
        matched = engine.find_matches(str(args.get("fact", "")))
        listing = "\n".join(f"- {m['content']}" for m in matched) or "(nothing)"
        return f"About to FORGET from memory:\n{listing}\nKept as history, out of every answer"
    if tool in ("home.turn_on", "home.turn_off"):
        name = str(args.get("entity", "")).split(".")[-1].replace("_", " ")
        name = name.upper() if len(name) <= 3 else name
        return f"About to turn the {name} {'ON' if tool.endswith('on') else 'OFF'}"
    return f"Run {tool} with {json.dumps(args)}?"


def _memory_block(message: str) -> str:
    """Engine-ranked memory (safety-critical + identity always, the rest
    by relevance and recency, budgeted) — falls back to the flat dump
    until the index exists."""
    from kyraan.memory import engine
    return (engine.build_context(message)
            or memory_store.load_all_facts()
            or "(no facts stored yet)")


async def run(chat_id: int, raw_text: str) -> str:
    """One agentic exchange. Returns the reply; raises AgentUnavailable to
    hand the message to the classifier fallback."""
    from kyraan.agents import orchestrator  # late: avoids a module cycle

    system = _AGENT_SYSTEM.format(
        capabilities=capability_brief(),
        tools=_tools_block(),
    )
    # Dynamic context lives AFTER the cache-stable system prefix. History
    # keeps its recent entries at full clip; older ones tighten — recency
    # carries the follow-up context, so nothing useful is dropped.
    transcript = (
        "CONTEXT:\n"
        f"Current date/time: {local_now().isoformat()}\n"
        "Known facts (owner-reviewed; [FLAGS] mark safety-relevant ones):\n"
        f"{_memory_block(raw_text)}\n"
        "Awaiting owner review:\n"
        f"{memory_store.load_pending_facts() or '(none)'}\n"
        "Conversation so far:\n"
        f"{orchestrator._history_block(chat_id, older_clip=250)}\n\n"
        f"USER: {raw_text}"
    )
    malformed_retries = 0
    calls_seen: dict = {}

    for step in range(_MAX_STEPS):
        try:
            response = await router.acall(prompt=transcript, system=system,
                                           tier="frontier", force_json=True)
        except router.ModelProviderError as exc:
            raise AgentUnavailable(str(exc)) from exc

        try:
            decision = json.loads(router.strip_code_fence(response.text))
            action = decision["action"]
        except (json.JSONDecodeError, KeyError, TypeError):
            malformed_retries += 1
            if malformed_retries > 1:
                raise AgentUnavailable(f"unparseable decision: {response.text[:200]}")
            transcript += "\nSYSTEM: that was not valid decision JSON — one JSON object only."
            continue

        consider = str(decision.get("consider", ""))[:200]

        if action == "reply":
            reply = str(decision.get("text", "")).strip()
            if not reply:
                raise AgentUnavailable("empty reply")
            log_event("agent_reply", chat_id=chat_id, steps=step + 1, consider=consider)
            return reply

        if action != "call" or decision.get("tool") not in TOOLS:
            malformed_retries += 1
            if malformed_retries > 1:
                raise AgentUnavailable(f"unknown action/tool: {response.text[:200]}")
            transcript += ("\nSYSTEM: unknown action or tool — use "
                           "{\"action\": \"reply\"|\"call\"} with a listed tool.")
            continue

        tool = decision["tool"]
        args = decision.get("args") or {}
        signature = f"{tool}:{json.dumps(args, sort_keys=True)}"
        repeats = calls_seen.get(signature, 0)
        if repeats >= 2:
            # Third identical call: the model is stuck — the classifier
            # fallback beats burning the whole step cap (seen live:
            # usage.report called 5x in a row past its own results).
            raise AgentUnavailable(f"stuck repeating {tool}")
        if repeats == 1:
            calls_seen[signature] = 2
            transcript += (f"\nSYSTEM: you already called {tool} with those exact args — "
                           "its result is above. Use it and reply to the user NOW.")
            continue
        calls_seen[signature] = 1
        log_event("agent_tool_call", chat_id=chat_id, tool=tool, step=step + 1,
                  consider=consider)
        try:
            result = await TOOLS[tool]["run"](chat_id, args, raw_text)
        except kernel.ConfirmationRequired:
            # The standard confirm flow, verbatim: stash the EXACT call;
            # the owner's yes replays it byte-identical through the kernel.
            captured_tool, captured_args = tool, dict(args)

            async def confirmed_handler(_a, _t=captured_tool, _ar=captured_args):
                outcome = await TOOLS[_t]["run"](chat_id, _ar, raw_text)
                return _confirmed_reply(_t, _ar, outcome)

            call = kernel.SkillCall("agent.action", {"tool": tool}, )
            return await orchestrator._gated(
                chat_id, call, confirmed_handler,
                describe=_describe_call(tool, args))
        except kernel.KillSwitchEngaged:
            raise
        except kernel.ToolFailed as exc:
            result = {"error": str(exc)}
        except Exception as exc:  # an executor bug must not brick the chat
            log_event("agent_tool_error", tool=tool, error=str(exc),
                      error_type=type(exc).__name__)
            # The REAL error goes back to the model — hiding it behind
            # "failed unexpectedly" left it retrying identical bad args
            # (seen live: days="few days", three blind retries).
            result = {"error": f"{tool}: {str(exc)[:200]}"}

        if tool == "email.unread":
            # Data boundary: with a cloud tier active, the history records
            # a placeholder instead of senders/subjects (same rule as the
            # classifier path).
            if orchestrator._cloud_tier_in_use():
                orchestrator._history_redaction.set("[showed the unread email summary]")

        rendered = json.dumps(result, ensure_ascii=False)
        transcript += f"\nTOOL {tool} -> {rendered[:2000]}"

    raise AgentUnavailable("step cap reached without a reply")


def _confirmed_reply(tool: str, args: dict, outcome) -> str:
    """Post-confirmation replies are templated, not model-composed — the
    loop ended at the ask; this is the receipt."""
    if isinstance(outcome, dict) and outcome.get("error"):
        return f"That failed: {outcome['error']}"
    if tool == "calendar.create_event":
        link = outcome.get("link", "") if isinstance(outcome, dict) else ""
        return f"Event created on your calendar: \"{args.get('title')}\" at {humanize(str(args.get('start')))}\n{link}".strip()
    if tool == "calendar.delete_event":
        if isinstance(outcome, dict) and outcome.get("already_gone"):
            return f"\"{args.get('title') or args.get('event_id')}\" was already gone."
        return f"Deleted from your calendar: \"{args.get('title') or args.get('event_id')}\""
    if tool == "memory.forget" and isinstance(outcome, dict):
        gone = outcome.get("forgotten") or []
        return "Forgotten: " + "; ".join(gone) if gone else "Nothing matched — nothing forgotten."
    if tool in ("home.turn_on", "home.turn_off"):
        wanted = "on" if tool.endswith("on") else "off"
        if isinstance(outcome, dict) and outcome.get("converged") is False:
            return (f"I sent the {wanted} command, but the device hasn't confirmed the "
                    f"switch yet — check it in a moment.")
        name = str(args.get("entity", "")).split(".")[-1].replace("_", " ")
        return f"Done — the {name} is {wanted}."
    return f"Done: {tool}."
