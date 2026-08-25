"""Central coordination point: every skill call and proactive send passes
through here so permissions, the kill switch, and logging can't be skipped
by forgetting to call them individually.
"""
from dataclasses import dataclass
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
    try:
        result = await handler(call.args)
        log_event("tool_result", skill=call.skill_name, ok=True)
        return result
    except Exception as exc:
        log_event("tool_result", skill=call.skill_name, ok=False, error=str(exc))
        raise


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
