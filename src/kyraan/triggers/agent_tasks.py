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

# Tasks advance via advance_occurrence, which rejects "interval" — the
# API refuses it up front so no confirmed record can crash later
# (audit round 2, P2; the loop_tools validator remains as the user-facing
# message, this is the belt under it).
TASK_REPEATS = tuple(r for r in REPEAT_CHOICES if r != "interval")

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
    # A produced-but-undelivered result (send failed): delivery retries
    # resend THIS instead of re-running the model — results survive send
    # failures without the duplicate-execution path (audit round 3, P1).
    pending_result: str = ""


def _load() -> list:
    try:
        return json.loads(TASKS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(records: list) -> None:
    TASKS_PATH.parent.mkdir(exist_ok=True)
    atomic_write_text(TASKS_PATH, json.dumps(records, indent=1, ensure_ascii=False))
    # P3.2d: mirror AFTER the file write; failures defer inside.
    from kyraan.store import promises
    promises.mirror_tasks(records)


def list_active(chat_id: int | None = None) -> list:
    from kyraan.store import promises
    records = promises.load_tasks() if promises.backend() == "pg" else None
    if records is None:
        records = _load()
    tasks = [AgentTask(**r) for r in records if r.get("active")]
    if chat_id is not None:
        tasks = [t for t in tasks if t.chat_id == chat_id]
    return tasks


def create(chat_id: int, instruction: str, when_iso: str, repeat: str = "") -> AgentTask:
    _parse_when(when_iso)  # validate before persisting
    if repeat and repeat not in TASK_REPEATS:
        raise ValueError(f"repeat must be one of {TASK_REPEATS} or empty — "
                         "interval recurrence is a reminder feature")
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
    """Cancel by id or unique id-prefix. An AMBIGUOUS prefix cancels
    NOTHING: the old loop deactivated every match, so a short prefix
    could silently retire several tasks at once (Bugbot P2)."""
    with locked(TASKS_PATH):
        records = _load()
        matches = [r for r in records
                   if r.get("active") and r["id"].startswith(task_id)]
        if len(matches) > 1:
            exact = [r for r in matches if r["id"] == task_id]
            if not exact:
                log_event("agent_task_cancel_ambiguous", task_id=task_id,
                          matches=len(matches))
                raise ValueError(
                    f"{len(matches)} active tasks start with {task_id!r} — "
                    "use the full id")
            matches = exact
        found = bool(matches)
        for record in matches:
            record["active"] = False
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


def _schedule_redelivery(task_id: str, minutes: int) -> None:
    """Keep a stashed recurring result alive across DND holds and repeat
    failures (Bugbot round-3 P2: the redeliver-only job consumed itself
    without rescheduling, leaving the result stale until next week)."""
    when = local_now() + timedelta(minutes=minutes)
    _schedule_fn(f"task-redeliver-{task_id}", when,
                 {"task_id": task_id, "redeliver_only": True})


def _retry_later(task: "AgentTask", minutes: int) -> None:
    """Reschedule AND persist the retry time — an in-memory-only backoff
    ran immediately after a restart (audit round 3, P2). Only for
    one-shots: a recurring series' next occurrence is its retry."""
    when = local_now() + timedelta(minutes=minutes)
    _advance(task.id, when.isoformat())
    _schedule_fn(f"task-{task.id}", when, {"task_id": task.id})


def _set_pending_result(task_id: str, text: str) -> None:
    with locked(TASKS_PATH):
        records = _load()
        for record in records:
            if record["id"] == task_id:
                record["pending_result"] = text
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
        if task.repeat and task.pending_result:
            # Redelivery survives restarts (Bugbot round-4 P2): the
            # redeliver-only job lives in the job queue's memory, but
            # the STASH lives in the store — a boot with a stashed
            # recurring result re-arms its redelivery instead of
            # waiting for next week's occurrence.
            _schedule_redelivery(task.id, 2)


async def fire(task_id: str, redeliver_only: bool = False) -> None:
    task = next((t for t in list_active() if t.id == task_id), None)
    if task is None:
        log_event("agent_task_fire_skipped", task_id=task_id, reason="cancelled or gone")
        return
    # recurring tasks advance FIRST — a failure or DND block skips this
    # occurrence rather than stalling the series. A redeliver-only fire
    # (Bugbot round-2 P2: a recurring task's stashed result must not
    # wait until next week's occurrence) never advances the series and
    # never runs fresh work — it exists only to flush pending_result.
    if task.repeat and not redeliver_only:
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
        if not task.repeat and not redeliver_only:
            _retry_later(task, 30)  # held through quiet hours, not lost
        elif task.repeat and task.pending_result:
            _schedule_redelivery(task.id, 30)  # survives the DND hold
        return
    if task.pending_result:
        # A produced result awaits delivery — resend it, NEVER re-run the
        # model. The label carries the ambiguity, exactly like reminders'
        # stale-lease takeover. Recurring tasks redeliver at their NEXT
        # occurrence and then continue with the fresh run (Bugbot P2:
        # their stashed results were previously dropped outright).
        try:
            await _send_fn(task.chat_id,
                           f"⏱ {task.pending_result}\n(may be a repeat — an "
                           "earlier delivery attempt failed mid-send)")
        except Exception as exc:
            log_event("agent_task_send_failed", task_id=task_id, error=str(exc)[:200])
            if not task.repeat:
                _retry_later(task, 5)
            else:
                _schedule_redelivery(task.id, 5)  # keeps retrying, never stale
            return
        log_event("agent_task_ran", task_id=task_id, redelivered=True)
        _set_pending_result(task.id, "")
        if not task.repeat:
            cancel(task.id)
            return
        if redeliver_only:
            return  # flushed; the series' own schedule does fresh work
        task = next((AgentTask(**r) for r in _load()
                     if r["id"] == task_id), task)  # fresh run continues
    try:
        result = await _run_fn(task.chat_id, task.instruction)
    except Exception as exc:
        log_event("agent_task_failed", task_id=task_id, error=str(exc)[:200])
        if not task.repeat:
            _retry_later(task, 15)  # transient failure: retry, don't discard
        return
    if not result or not str(result).strip():
        # Both model tiers down surfaces as an EMPTY result, not an
        # exception — treating it as success cancelled the one-shot
        # (audit round 2, P1). Same retry path as a raise.
        log_event("agent_task_empty_result", task_id=task_id)
        if not task.repeat:
            _retry_later(task, 15)
        return
    try:
        await _send_fn(task.chat_id, f"⏱ {result}")
    except Exception as exc:
        # The send failed but the RUN happened. Stash the result and retry
        # DELIVERY only — a definite pre-delivery failure is no longer a
        # permanent loss (audit round 3, P1), and the duplicate-execution
        # path stays closed; the may-be-a-repeat label covers the
        # ambiguous case.
        log_event("agent_task_send_failed", task_id=task_id, error=str(exc)[:200])
        _set_pending_result(task.id, str(result))
        if not task.repeat:
            _retry_later(task, 5)
        else:
            # a recurring task's result is redelivered in minutes via a
            # redeliver-only fire, not at next week's occurrence
            _schedule_redelivery(task.id, 5)
        return
    log_event("agent_task_ran", task_id=task_id)
    if not task.repeat:
        cancel(task.id)
