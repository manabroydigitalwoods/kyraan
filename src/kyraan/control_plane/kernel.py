"""Central coordination point: every skill call, tool call, and proactive
send passes through here so permissions, the kill switch, and logging can't
be skipped by forgetting to call them individually.
"""
import asyncio
import contextvars
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from kyraan.control_plane import config, dnd, kill_switch
from kyraan.control_plane.logging_setup import log_event


class KillSwitchEngaged(Exception):
    pass


class ConfirmationRequired(Exception):
    """Raised when a skill needs confirm-first approval that hasn't been given."""

    def __init__(self, skill_name: str, args: dict):
        self.skill_name = skill_name
        self.args = args
        super().__init__(f"'{skill_name}' requires confirmation before running")


@dataclass
class SkillCall:
    skill_name: str
    args: dict
    confirmed: bool = False


# True while executing a skill the user explicitly confirmed. A confirm-
# gated tool inside that skill doesn't re-prompt — the user already said
# yes to this exact action; asking twice for one intent is friction, not
# safety. Contextvar (not a global) so concurrent chats can't leak
# confirmation into each other.
_skill_confirmed: contextvars.ContextVar[bool] = contextvars.ContextVar("skill_confirmed", default=False)

# Loop-engineering rails (plan §3b): every skill invocation gets a hard
# tool-step budget, and an identical (tool, args) repeat breaks out
# instead of spinning — both tied to the skill run via a contextvar, so
# concurrent chats meter independently. 8, not the design sketch's 5,
# because home.query legitimately reads 5 sensors in one pass.
_MAX_TOOL_STEPS = 8
_tool_steps: contextvars.ContextVar[list | None] = contextvars.ContextVar("tool_steps", default=None)


def confirmed_context() -> bool:
    """True while executing inside a skill the user explicitly confirmed —
    lets agent-loop executors implement confirm-gated actions that aren't
    registry tools (e.g. memory.forget)."""
    return _skill_confirmed.get()


# --- P3.5b: per-stage tool scoping (arch §1 two-layer pattern) ------------
# The VIEWER'S stage for the current turn — set at the channel boundary,
# default "owner" so every internal path (scheduled runs, proactive
# sends, scripts) keeps full capability. Non-owner stages execute ONLY
# what config's stage_toolsets allowlist names; the menu filter in the
# agent loop is the polite layer, this is the wall — a model asking for
# an out-of-scope tool by any phrasing is refused HERE.
_viewer = contextvars.ContextVar("kyraan_viewer", default=("owner", "owner"))


def viewer_stage() -> str:
    return _viewer.get()[1]


def viewer_person() -> str:
    """WHO is looking this turn — 'owner' by default; the §4 visibility
    clause keys on this (P3.5c)."""
    return _viewer.get()[0]


def effective_reviewer() -> str | None:
    """The person this turn's memory writes/reads belong to, FAIL-CLOSED
    (2026-08-28 sweep after the Ruma identity incident): an empty viewer
    person inherits owner ONLY when the stage itself says owner — a
    stage-only viewer gets None, and every caller must treat None as
    'refuse', never as 'the owner'."""
    person, stage = _viewer.get()
    if person and person != "none":  # "none" is set_viewer's empty encoding
        return person
    return "owner" if stage == "owner" else None


def set_viewer(person: str, stage: str):
    return _viewer.set((person or "none", stage or "none"))


def set_viewer_stage(stage: str):
    """Stage-only form (P3.5b tests/back-compat): a non-owner stage with
    no named person gets the empty viewer — sees shared facts only."""
    stage = stage or "none"
    return _viewer.set(("owner" if stage == "owner" else "", stage))


def reset_viewer_stage(token) -> None:
    _viewer.reset(token)


def stage_allows(name: str, stage: str | None = None) -> bool:
    """Effective access = the stage's toolset ∪ the OWNER'S individual
    grants for this viewer (person.extra_tools — "give ruma photo
    upload" without inventing a role; owner directive 2026-08-28).
    Grants apply only when checking the CURRENT viewer (an explicit
    `stage=` probe, as the frozen-surface test uses, stays pure-role)."""
    explicit = stage is not None
    stage = stage if explicit else viewer_stage()
    if stage == "owner":
        return True
    allowed = (config.load().get("stage_toolsets") or {}).get(stage) or []
    # entries are exact names or prefixes ("reminders" covers reminders.*)
    if any(name == entry or name.startswith(entry + ".")
           for entry in allowed):
        return True
    person = effective_reviewer()
    if not person or person == "owner":
        return False
    if explicit and stage != viewer_stage():
        return False  # pure-role probe (the frozen-surface test's mode)
    try:
        from kyraan.store import persons
        return name in persons.extra_tools(person)
    except Exception:
        return False  # fail-closed


def _stage_gate(kind: str, name: str, args) -> None:
    if not stage_allows(name):
        log_event("blocked_stage_scope", stage=viewer_stage(),
                  **{kind: name}, args=args)
        raise ToolFailed(
            f"'{name}' is not available at your access level — "
            "ask the owner if you need it")


async def run_skill(call: SkillCall, handler: Callable[[dict], Awaitable[object]]) -> object:
    """Gate + execute a skill. `handler` does the actual work."""
    if kill_switch.is_engaged():
        log_event("blocked_kill_switch", skill=call.skill_name, args=call.args)
        raise KillSwitchEngaged("Kill switch is engaged — all autonomous action halted")
    _stage_gate("skill", call.skill_name, call.args)

    skill_cfg = config.skill_config(call.skill_name)
    if skill_cfg["permission"] == "confirm" and not call.confirmed:
        log_event("confirmation_required", skill=call.skill_name, args=call.args)
        raise ConfirmationRequired(call.skill_name, call.args)

    log_event("tool_call", skill=call.skill_name, args=call.args, permission=skill_cfg["permission"])
    token = _skill_confirmed.set(call.confirmed)
    steps_token = _tool_steps.set([])
    try:
        result = await handler(call.args)
        log_event("tool_result", skill=call.skill_name, ok=True)
        return result
    except Exception as exc:
        log_event("tool_result", skill=call.skill_name, ok=False, error=str(exc))
        raise
    finally:
        _skill_confirmed.reset(token)
        _tool_steps.reset(steps_token)


class ToolFailed(Exception):
    """A registered tool exhausted its failure policy — the message is safe
    to show the user (on_failure: surface)."""


@dataclass
class ToolCall:
    tool_name: str
    args: dict
    confirmed: bool = False


def _validate_args(spec, args: dict) -> None:
    for pname, pspec in spec.params.items():
        if pspec.get("required") and pname not in args:
            raise ToolFailed(f"tool {spec.name!r}: missing required parameter {pname!r}")
    for pname, value in args.items():
        pspec = spec.params.get(pname)
        if pspec is None:
            raise ToolFailed(f"tool {spec.name!r}: unexpected parameter {pname!r}")
        ptype = pspec["type"]
        ok = (
            (ptype == "string" and isinstance(value, str))
            or (ptype == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
            or (ptype == "bool" and isinstance(value, bool))
            or (ptype == "datetime" and isinstance(value, str) and _parses_as_datetime(value))
        )
        if not ok:
            raise ToolFailed(f"tool {spec.name!r}: parameter {pname!r} is not a valid {ptype}")


def _parses_as_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


async def run_tool(call: ToolCall, _allow_fallback: bool = True) -> object:
    """Gate + execute a registered tool: kill switch, permission, param
    validation against the registry schema, audit logging, and the entry's
    timeout/retry/failure policy. Model-generated args never reach an
    adapter unvalidated."""
    from kyraan.tools import registry  # deferred: tools layer sits above the kernel

    spec = registry.get(call.tool_name)

    if kill_switch.is_engaged():
        log_event("blocked_kill_switch", tool=spec.name, args=call.args)
        raise KillSwitchEngaged("Kill switch is engaged — all autonomous action halted")
    _stage_gate("tool", spec.name, call.args)
    if spec.permission == "disabled":
        raise ToolFailed(f"tool {spec.name!r} is disabled in config/permissions.yaml")
    if spec.permission == "confirm" and not (call.confirmed or _skill_confirmed.get()):
        log_event("confirmation_required", tool=spec.name, args=call.args)
        raise ConfirmationRequired(spec.name, call.args)

    steps = _tool_steps.get()
    if steps is not None:
        import json as _json

        signature = (spec.name, _json.dumps(call.args, sort_keys=True, default=str))
        if signature in steps:
            log_event("tool_loop_detected", tool=spec.name, args=call.args)
            raise ToolFailed(
                f"stopped: {spec.name} was about to run twice with identical arguments — "
                "that's a loop, not progress; tell me differently what you need"
            )
        if len(steps) >= _MAX_TOOL_STEPS:
            log_event("tool_step_limit", tool=spec.name, steps=len(steps))
            raise ToolFailed(
                f"stopped: this request hit the {_MAX_TOOL_STEPS}-tool-call safety limit for one action"
            )
        steps.append(signature)

    _validate_args(spec, call.args)
    log_event("tool_call", tool=spec.name, args=call.args, permission=spec.permission)

    import time as _time
    started = _time.monotonic()
    last_exc: Exception | None = None
    for attempt in range(spec.retries + 1):
        try:
            result = await asyncio.wait_for(registry.dispatch(spec, call.args), timeout=spec.timeout_s)
            duration = round((_time.monotonic() - started) * 1000)
            log_event("tool_result", tool=spec.name, ok=True, attempt=attempt,
                      duration_ms=duration)
            from kyraan.control_plane.logging_setup import record_stage
            record_stage(f"tool:{spec.name}", duration)
            return result
        except asyncio.TimeoutError as exc:
            if spec.side_effects == "write":
                # A timeout CANNOT cancel work already running in the
                # adapter's worker thread — the command may still land
                # after this failure (external review P1). Say so, and
                # never retry a write whose outcome is unknown: that is
                # how duplicates happen.
                log_event("tool_result", tool=spec.name, ok=False, error="timeout, outcome unknown")
                raise ToolFailed(
                    f"{spec.name} timed out — the command MAY still have gone "
                    "through; check the actual state before retrying") from exc
            last_exc = exc
            log_event("tool_retry", tool=spec.name, attempt=attempt, error=str(exc))
        except registry.TransientToolError as exc:
            last_exc = exc
            log_event("tool_retry", tool=spec.name, attempt=attempt, error=str(exc))
        except registry.ToolError as exc:
            # Non-transient: retrying can't help, apply the failure policy now.
            last_exc = exc
            break

    log_event("tool_result", tool=spec.name, ok=False, error=str(last_exc),
              error_name=registry.error_name(last_exc),
              duration_ms=round((_time.monotonic() - started) * 1000))
    if spec.on_failure.startswith("fallback:") and _allow_fallback:
        # One fallback hop only — a fallback's own fallback never runs, so
        # a config cycle (A -> B -> A) can't loop.
        fallback_name = spec.on_failure.split(":", 1)[1]
        log_event("tool_fallback", tool=spec.name, fallback=fallback_name)
        return await run_tool(ToolCall(fallback_name, call.args, confirmed=call.confirmed), _allow_fallback=False)
    if spec.on_failure == "silent":
        return None
    raise ToolFailed(f"{spec.name} failed: {last_exc}")


def can_send_proactively(force: bool = False, chat_id: int | None = None) -> bool:
    """Gate for reminders/briefs/curiosity questions. `force` bypasses DND
    only — never the kill switch. `chat_id` (P3.5d) additionally honors
    the recipient PERSON's own quiet hours when they have any — the
    global window always applies on top."""
    if kill_switch.is_engaged():
        log_event("blocked_kill_switch", context="proactive_send")
        return False
    if not force and dnd.in_quiet_hours():
        log_event("blocked_dnd", context="proactive_send")
        return False
    if not force and chat_id is not None:
        from kyraan.store import persons
        window = persons.dnd_window(chat_id)
        if window and _in_person_window(*window):
            log_event("blocked_dnd", context="proactive_send",
                      chat_id=chat_id, person_window=True)
            return False
    return True


def _in_person_window(start: str, end: str) -> bool:
    """Wraparound-safe "HH:MM" window check against the local clock."""
    try:
        now = dnd.local_now().strftime("%H:%M")
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # crosses midnight
    except Exception:
        return False  # a malformed window never silences sends
