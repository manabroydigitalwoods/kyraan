"""Goal continuity (owner decisions 2026-08-31, docs/design/
goal_continuity.md): a PURSUIT that survives across days — steps, an
append-only journal of findings, and a daily read-only work cycle that
researches the open steps and pings only on real progress.

Doctrine carried over whole: creation confirm-gated; cycles read-only
(research + propose, never act); pings edge-style (NOTHING_NEW is
dropped); goals chat-scoped like reminders — the owner does not see an
enrolled person's goals; a person's cycle runs AS that person (viewer
context set for the whole run, never inherited from the owner default).
"""
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.filelock import atomic_write_text, locked
from kyraan.control_plane.logging_setup import log_event

GOALS_PATH = Path(__file__).resolve().parents[3] / "data" / "goals.json"
# File store only for now (rides the nightly tar); a PG mirror joins
# when goals prove themselves — same deliberate note as event_rules.

MAX_ACTIVE_PER_PERSON = 3
MAX_CYCLES_PER_DAY = 2          # hard ceiling; cadence default is 1/day
_STATUSES = ("active", "paused", "done")


@dataclass
class Goal:
    id: str
    chat_id: int
    person: str                 # registry person id — the goal's OWNER
    stage: str                  # their stage at creation; cycles run at it
    title: str
    why: str = ""
    steps: list = field(default_factory=list)    # [{text, done, note}]
    journal: list = field(default_factory=list)  # [{ts, text}]
    status: str = "active"
    cadence_hours: int = 24
    next_cycle_iso: str = ""
    cycles_today: int = 0
    cycles_date: str = ""
    # An appended-but-unpinged finding (send failed): the next cycle's
    # ping carries it rather than re-running the research — journal is
    # the truth, delivery retries.
    unreported: str = ""


def _load() -> list:
    try:
        return json.loads(GOALS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(records: list) -> None:
    GOALS_PATH.parent.mkdir(exist_ok=True)
    atomic_write_text(GOALS_PATH, json.dumps(records, indent=1,
                                             ensure_ascii=False))


def list_for(chat_id: int, status: str | None = "active") -> list:
    goals = [Goal(**r) for r in _load() if r["chat_id"] == chat_id]
    if status:
        goals = [g for g in goals if g.status == status]
    return goals


def get(goal_id: str) -> Goal | None:
    row = next((r for r in _load() if r["id"] == goal_id), None)
    return Goal(**row) if row else None


def resolve(chat_id: int, ref: str) -> Goal:
    """Find ONE goal by id-prefix or title words — ambiguity refuses,
    like agent_tasks.cancel (a short ref must never act on several)."""
    ref = ref.strip().lower()
    mine = [Goal(**r) for r in _load()
            if r["chat_id"] == chat_id and r["status"] != "done"]
    hits = [g for g in mine if g.id.startswith(ref)] or \
           [g for g in mine if ref in g.title.lower()]
    if not hits:
        raise ValueError(f"no goal matches {ref!r} — list goals first")
    if len(hits) > 1:
        raise ValueError(f"{len(hits)} goals match {ref!r} — "
                         "use more words or the id")
    return hits[0]


def create(chat_id: int, person: str, stage: str, title: str,
           why: str = "", steps: list | None = None,
           cadence_hours: int = 24) -> Goal:
    title = title.strip()
    if not 3 <= len(title) <= 120:
        raise ValueError("give the goal a short, clear title")
    if len(list_for(chat_id)) >= MAX_ACTIVE_PER_PERSON:
        raise ValueError(f"{MAX_ACTIVE_PER_PERSON} goals are already "
                         "active — finish or pause one first")
    cadence_hours = max(12, min(int(cadence_hours or 24), 24 * 7))
    goal = Goal(id=uuid.uuid4().hex[:8], chat_id=chat_id,
                person=person or "owner", stage=stage or "owner",
                title=title, why=why.strip()[:400],
                steps=[{"text": str(s).strip()[:200], "done": False,
                        "note": ""} for s in (steps or []) if str(s).strip()],
                cadence_hours=cadence_hours,
                next_cycle_iso=(local_now()
                                + timedelta(hours=cadence_hours)).isoformat())
    with locked(GOALS_PATH):
        records = _load()
        records.append(asdict(goal))
        _save(records)
    _schedule(goal)
    log_event("goal_created", goal_id=goal.id, chat_id=chat_id,
              steps=len(goal.steps), cadence_hours=cadence_hours)
    return goal


def _mutate(goal_id: str, fn) -> Goal:
    with locked(GOALS_PATH):
        records = _load()
        row = next((r for r in records if r["id"] == goal_id), None)
        if row is None:
            raise ValueError("that goal is gone")
        fn(row)
        _save(records)
    return Goal(**row)


def set_status(chat_id: int, ref: str, status: str) -> tuple:
    """Returns (goal, prior_status) — the prior feeds undo."""
    if status not in _STATUSES:
        raise ValueError(f"status must be one of {_STATUSES}")
    goal = resolve(chat_id, ref)
    prior = goal.status
    if prior == status:
        raise ValueError(f"already {status}")

    def apply(row):
        row["status"] = status
        if status == "active":
            row["next_cycle_iso"] = (
                local_now() + timedelta(hours=row["cadence_hours"])).isoformat()
    goal = _mutate(goal.id, apply)
    if status == "active":
        _schedule(goal)
    log_event("goal_status", goal_id=goal.id, status=status, prior=prior)
    return goal, prior


def update(chat_id: int, ref: str, step_done: str = "",
           add_step: str = "", note: str = "",
           reopen_step: str = "") -> Goal:
    """Conversational progress: check a step off (by words), add a step,
    or append a journal note — internal state, auto permission."""
    goal = resolve(chat_id, ref)
    if not (step_done or add_step or note or reopen_step):
        raise ValueError("say what changed: a step done, a new step, "
                         "or a note")

    def apply(row):
        if step_done:
            wanted = step_done.strip().lower()
            hits = [s for s in row["steps"]
                    if not s["done"] and wanted in s["text"].lower()]
            if not hits:
                raise ValueError(f"no open step matches {step_done!r} — "
                                 "show the goal first")
            if len(hits) > 1:
                raise ValueError(f"{len(hits)} open steps match "
                                 f"{step_done!r} — use more words")
            hits[0]["done"] = True
        if reopen_step:
            wanted = reopen_step.strip().lower()
            hits = [s2 for s2 in row["steps"]
                    if s2["done"] and wanted in s2["text"].lower()]
            if len(hits) != 1:
                raise ValueError(f"can't reopen {reopen_step!r} — "
                                 f"{len(hits)} done steps match")
            hits[0]["done"] = False
        if add_step:
            row["steps"].append({"text": add_step.strip()[:200],
                                 "done": False, "note": ""})
        if note:
            row["journal"].append({"ts": local_now().isoformat(),
                                   "text": note.strip()[:1500]})
    goal = _mutate(goal.id, apply)
    log_event("goal_updated", goal_id=goal.id,
              step_done=bool(step_done), add_step=bool(add_step),
              note=bool(note))
    return goal


def brief_line(chat_id: int) -> str:
    """One line for the owner's morning brief — active goals only."""
    goals = list_for(chat_id)
    if not goals:
        return ""
    parts = []
    for g in goals:
        done = sum(1 for s in g.steps if s["done"])
        nxt = next((s["text"] for s in g.steps if not s["done"]), None)
        piece = f"{g.title} — {done}/{len(g.steps)} steps" if g.steps \
            else g.title
        if nxt:
            piece += f", next: {nxt}"
        parts.append(piece)
    return "🎯 " + "; ".join(parts)


# --- the work cycle -------------------------------------------------------

_schedule_fn = None
_run_fn = None
_send_fn = None


def init(schedule_fn, run_fn, send_fn) -> None:
    """run_fn(goal) -> str executes the research instruction READ-ONLY
    with the goal person's viewer context (the channel wires that);
    send_fn(chat_id, text) -> bool delivers with the standard truth."""
    global _schedule_fn, _run_fn, _send_fn
    _schedule_fn, _run_fn, _send_fn = schedule_fn, run_fn, send_fn
    for goal in [Goal(**r) for r in _load() if r["status"] == "active"]:
        _schedule(goal)


def _schedule(goal: Goal) -> None:
    if _schedule_fn is None:
        return  # tool-created before channel wiring only happens in tests
    try:
        when = datetime.fromisoformat(goal.next_cycle_iso)
    except ValueError:
        when = local_now() + timedelta(hours=goal.cadence_hours)
    if when < local_now():
        when = local_now() + timedelta(minutes=5)
    _schedule_fn(f"goal-{goal.id}", when, {"goal_id": goal.id})


def cycle_instruction(goal: Goal) -> str:
    steps_txt = "\n".join(
        f"- [{'x' if s['done'] else ' '}] {s['text']}"
        + (f" ({s['note']})" if s.get("note") else "")
        for s in goal.steps) or "(no steps yet — propose 3-5)"
    journal_txt = "\n".join(
        f"[{e['ts'][:10]}] {e['text']}" for e in goal.journal[-6:]) \
        or "(nothing yet)"
    return (
        f"GOAL WORK CYCLE (read-only research). Goal: {goal.title}.\n"
        f"Why: {goal.why or '(not stated)'}\nSteps:\n{steps_txt}\n"
        f"Journal (recent findings):\n{journal_txt}\n\n"
        "Research the OPEN steps with your read-only tools (web, "
        "calendar, weather, places). Report ONLY genuinely new, "
        "concrete findings as short bullets; you may end with ONE "
        "suggested next action for the user to approve. If a finding "
        "FULLY SETTLES an open step, add a final line 'STEP_DONE: "
        "<words of that step>'; if it reveals a concrete new step, add "
        "'STEP_ADD: <short step>' (max 2). Never invent progress — a "
        "step is done only when the finding proves it. If nothing new "
        "was found, reply exactly NOTHING_NEW.")


async def fire(goal_id: str) -> None:
    goal = get(goal_id)
    if goal is None or goal.status != "active":
        log_event("goal_cycle_skipped", goal_id=goal_id,
                  reason="gone or not active")
        return
    # advance FIRST — a failed cycle skips to the next occurrence
    # rather than stalling the cadence (the agent_tasks lesson).
    next_when = local_now() + timedelta(hours=goal.cadence_hours)
    today = local_now().date().isoformat()
    cycles = goal.cycles_today if goal.cycles_date == today else 0
    if cycles >= MAX_CYCLES_PER_DAY:
        log_event("goal_cycle_capped", goal_id=goal_id)
        _mutate(goal_id, lambda r: r.update(next_cycle_iso=next_when.isoformat()))
        _schedule(get(goal_id))
        return
    _mutate(goal_id, lambda r: r.update(
        next_cycle_iso=next_when.isoformat(),
        cycles_today=cycles + 1, cycles_date=today))
    _schedule(get(goal_id))
    if not kernel.can_send_proactively(chat_id=goal.chat_id):
        # A held cycle is just skipped — goals are long-horizon; the
        # next cadence slot comes soon enough. Nothing is lost: an
        # unreported finding would already be in the journal.
        log_event("goal_cycle_held_dnd", goal_id=goal_id)
        return
    try:
        result = str(await _run_fn(goal) or "").strip()
    except Exception as exc:
        log_event("goal_cycle_failed", goal_id=goal_id, error=str(exc)[:150])
        return
    finding = "" if (not result or "NOTHING_NEW" in result[:400]) else result
    step_notes = []
    if finding:
        # The cycle keeps the goal's own books (owner "go", 2026-09-01):
        # structured STEP_DONE/STEP_ADD lines are parsed and applied
        # DETERMINISTICALLY through the same machinery conversational
        # updates use — bounded (1 done, 2 adds per cycle), matched
        # against real open steps, ignored-and-logged on any mismatch,
        # never guessed. Internal goal state only; the outside world
        # still sees zero writes from a cycle.
        import re as _re
        done = _re.findall(r"^STEP_DONE:\s*(.+)$", finding, _re.MULTILINE)[:1]
        adds = _re.findall(r"^STEP_ADD:\s*(.+)$", finding, _re.MULTILINE)[:2]
        finding = _re.sub(r"^STEP_(?:DONE|ADD):.*$", "", finding,
                          flags=_re.MULTILINE).strip()
        for wanted in done:
            wanted_l = wanted.strip().lower()

            def _check(row, w=wanted_l):
                hits = [st for st in row["steps"]
                        if not st["done"] and w in st["text"].lower()]                     or [st for st in row["steps"]
                        if not st["done"]
                        and st["text"].lower() in w]
                if len(hits) != 1:
                    raise ValueError("no unique open step")
                hits[0]["done"] = True
                step_notes.append(f"✔ step done: {hits[0]['text']}")
            try:
                _mutate(goal_id, _check)
            except ValueError:
                log_event("goal_cycle_step_ignored", goal_id=goal_id,
                          step_done=wanted[:60])
        for new_step in adds:
            text = new_step.strip()[:200]
            if len(text) < 4:
                continue
            existing = {st["text"].lower() for st in
                        (get(goal_id) or goal).steps}
            if text.lower() in existing:
                continue
            _mutate(goal_id, lambda r, t=text: r["steps"].append(
                {"text": t, "done": False, "note": ""}))
            step_notes.append(f"+ step added: {text}")
        if step_notes:
            log_event("goal_cycle_steps", goal_id=goal_id,
                      applied=len(step_notes))
    carry = goal.unreported
    if finding:
        digest = hashlib.sha256(finding.encode()).hexdigest()[:16]
        recent = {hashlib.sha256(e["text"].encode()).hexdigest()[:16]
                  for e in goal.journal[-10:]}
        if digest in recent:
            finding = ""  # the model re-found what the journal holds
        else:
            _mutate(goal_id, lambda r: r["journal"].append(
                {"ts": local_now().isoformat(), "text": finding[:1500]}))
    to_report = "\n".join(t for t in (carry, finding,
                                       "\n".join(step_notes)) if t)
    if not to_report:
        log_event("goal_cycle_quiet", goal_id=goal_id)
        return
    text = f"🎯 {goal.title}:\n{to_report[:2500]}"
    delivered = await _send_fn(goal.chat_id, text)
    if delivered is False:
        # journal keeps the finding; the NEXT cycle's ping carries it
        _mutate(goal_id, lambda r: r.update(unreported=to_report[:2500]))
        log_event("goal_report_undelivered", goal_id=goal_id)
        return
    if carry:
        _mutate(goal_id, lambda r: r.update(unreported=""))
    log_event("goal_cycle_reported", goal_id=goal_id)
