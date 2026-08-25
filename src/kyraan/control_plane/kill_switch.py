"""File-based kill switch, independent of every other guardrail.

Touching KILL_SWITCH_PATH halts all autonomous action (proactive triggers,
skill execution) immediately. Deleting it resumes. Checked by the kernel
before any tool call or proactive send — never bypassed by config.
"""
from pathlib import Path

KILL_SWITCH_PATH = Path(__file__).resolve().parents[3] / "KILL_SWITCH"


def is_engaged() -> bool:
    return KILL_SWITCH_PATH.exists()


def engage(reason: str = "") -> None:
    KILL_SWITCH_PATH.write_text(reason or "engaged with no reason given\n")


def disengage() -> None:
    KILL_SWITCH_PATH.unlink(missing_ok=True)
