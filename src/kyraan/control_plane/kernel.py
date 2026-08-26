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


async def run_skill(call: SkillCall, handler: Callable[[dict], Awaitable[object]]) -> object:
    """Gate + execute a skill. `handler` does the actual work."""
    if kill_switch.is_engaged():
        log_event("blocked_kill_switch", skill=call.skill_name, args=call.args)
        raise KillSwitchEngaged("Kill switch is engaged — all autonomous action halted")

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

    last_exc: Exception | None = None
    for attempt in range(spec.retries + 1):
        try:
            result = await asyncio.wait_for(registry.dispatch(spec, call.args), timeout=spec.timeout_s)
            log_event("tool_result", tool=spec.name, ok=True, attempt=attempt)
            return result
        except (registry.TransientToolError, asyncio.TimeoutError) as exc:
            last_exc = exc
            log_event("tool_retry", tool=spec.name, attempt=attempt, error=str(exc))
        except registry.ToolError as exc:
            # Non-transient: retrying can't help, apply the failure policy now.
            last_exc = exc
            break

    log_event("tool_result", tool=spec.name, ok=False, error=str(last_exc))
    if spec.on_failure.startswith("fallback:") and _allow_fallback:
        # One fallback hop only — a fallback's own fallback never runs, so
        # a config cycle (A -> B -> A) can't loop.
        fallback_name = spec.on_failure.split(":", 1)[1]
        log_event("tool_fallback", tool=spec.name, fallback=fallback_name)
        return await run_tool(ToolCall(fallback_name, call.args, confirmed=call.confirmed), _allow_fallback=False)
    if spec.on_failure == "silent":
        return None
    raise ToolFailed(f"{spec.name} failed: {last_exc}")


def can_send_proactively(force: bool = False) -> bool:
    """Gate for reminders/briefs/curiosity questions. `force` bypasses DND
    only — never the kill switch."""
    if kill_switch.is_engaged():
        log_event("blocked_kill_switch", context="proactive_send")
        return False
    if not force and dnd.in_quiet_hours():
        log_event("blocked_dnd", context="proactive_send")
        return False
    return True
