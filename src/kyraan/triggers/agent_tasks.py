"""Scheduled agent tasks — instructions executed by the agent loop at a
set time (harness pack A). Unlike a reminder (static text), a task RUNS:
"every evening at 8, check tomorrow's calendar and warn me about early
meetings" fetches the live calendar at 8 and composes the warning.

Safety, by construction:
- scheduled runs get READ-ONLY tools (the loop's read_only mode) — a
  write can only happen live, behind the owner's confirm
- creating a task is itself confirm-gated (a standing autonomous
  behavior deserves a yes)
- every send passes kernel.can_send_proactively (kill switch + DND);
  a blocked recurring run skips to its next occurrence
"""
import json
import uuid
from datetime import timedelta
from dataclasses import asdict, dataclass
from pathlib import Path

from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.filelock import atomic_write_text, locked
from kyraan.control_plane.logging_setup import log_event
from kyraan.triggers.scheduler import REPEAT_CHOICES, _parse_when, advance_occurrence

TASKS_PATH = Path(__file__).resolve().parents[3] / "data" / "agent_tasks.json"

_schedule_fn = None
_run_fn = None
_send_fn = None


@dataclass
class AgentTask:
    id: str
    chat_id: int
    instruction: str
    when_iso: str
    repeat: str = ""
    active: bool = True


def _load() -> list:
    try:
        return json.loads(TASKS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(records: list) -> None:
    TASKS_PATH.parent.mkdir(exist_ok=True)
    atomic_write_text(TASKS_PATH, json.dumps(records, indent=1, ensure_ascii=False))


def list_active(chat_id: int | None = None) -> list:
    tasks = [AgentTask(**r) for r in _load() if r.get("active")]
    if chat_id is not None:
        tasks = [t for t in tasks if t.chat_id == chat_id]
    return tasks


def create(chat_id: int, instruction: str, when_iso: str, repeat: str = "") -> AgentTask:
    _parse_when(when_iso)  # validate before persisting
    if repeat and repeat not in REPEAT_CHOICES:
        raise ValueError(f"repeat must be one of {REPEAT_CHOICES} or empty")
    task = AgentTask(id=uuid.uuid4().hex[:8], chat_id=chat_id,
                     instruction=instruction, when_iso=when_iso, repeat=repeat)
    with locked(TASKS_PATH):
        records = _load()
        records.append(asdict(task))
        _save(records)
    _schedule(task)
    log_event("agent_task_created", task_id=task.id, when=when_iso, repeat=repeat or None)
    return task


def cancel(task_id: str) -> bool:
    with locked(TASKS_PATH):
        records = _load()
        found = False
        for record in records:
            if record["id"].startswith(task_id) and record.get("active"):
                record["active"] = False
                found = True
        _save(records)
    if found:
        log_event("agent_task_cancelled", task_id=task_id)
    return found


def _advance(task_id: str, next_iso: str) -> None:
    with locked(TASKS_PATH):
        records = _load()
        for record in records:
            if record["id"] == task_id:
                record["when_iso"] = next_iso
        _save(records)


def _schedule(task: AgentTask) -> None:
    assert _schedule_fn is not None, "agent_tasks.init() first"
    when = _parse_when(task.when_iso)
    if when < local_now():
        if task.repeat:
            # A single advance after downtime can still land in the past —
            # rescheduling a past time refires immediately, repeatedly
            # (Bugbot P1). Skip every missed occurrence and PERSIST the
            # new base so fire() advances from a future anchor.
            while when < local_now():
                when = advance_occurrence(when, task.repeat)
            _advance(task.id, when.isoformat())
        else:
            when = local_now()
    _schedule_fn(f"task-{task.id}", when, {"task_id": task.id})


def init(schedule_fn, run_fn, send_fn, only_chat: int | None = None) -> None:
    """run_fn(chat_id, instruction) -> str executes the instruction with
    READ-ONLY tools; send_fn delivers the result."""
    global _schedule_fn, _run_fn, _send_fn
    _schedule_fn, _run_fn, _send_fn = schedule_fn, run_fn, send_fn
    for task in list_active(only_chat):
        try:
            _schedule(task)
        except ValueError as exc:
            log_event("agent_task_schedule_failed", task_id=task.id, error=str(exc))


async def fire(task_id: str) -> None:
    task = next((t for t in list_active() if t.id == task_id), None)
    if task is None:
        log_event("agent_task_fire_skipped", task_id=task_id, reason="cancelled or gone")
        return
    # recurring tasks advance FIRST — a failure or DND block skips this
    # occurrence rather than stalling the series
    if task.repeat:
        next_when = advance_occurrence(_parse_when(task.when_iso), task.repeat)
        while next_when <= local_now():  # stale base after downtime: catch up
            next_when = advance_occurrence(next_when, task.repeat)
        _advance(task.id, next_when.isoformat())
        _schedule_fn(f"task-{task.id}", next_when, {"task_id": task.id})
    # One-shots retire only AFTER a successful run — retiring first meant a
    # DND hold or a transient model/send failure permanently discarded the
    # task (Bugbot P1).
    if not kernel.can_send_proactively():
        log_event("agent_task_skipped_dnd", task_id=task_id)
        if not task.repeat:
            _schedule_fn(f"task-{task.id}", local_now() + timedelta(minutes=30),
                         {"task_id": task.id})  # held through quiet hours, not lost
        return
    try:
        result = await _run_fn(task.chat_id, task.instruction)
        if result:
            await _send_fn(task.chat_id, f"⏱ {result}")
        log_event("agent_task_ran", task_id=task_id)
    except Exception as exc:
        log_event("agent_task_failed", task_id=task_id, error=str(exc)[:200])
        if not task.repeat:
            _schedule_fn(f"task-{task.id}", local_now() + timedelta(minutes=15),
                         {"task_id": task.id})  # transient failure: retry, don't discard
        return
    if not task.repeat:
        cancel(task.id)
