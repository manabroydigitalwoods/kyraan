"""Wake scheduling (§3d gap #4, built 2026-08-31): this Mac sleeps, and
a sleeping Mac fires nothing. The misfire fix (2026-08-30) made missed
jobs fire LATE instead of never — Kiaan's 5 AM vaccination reminder
arrived hours after the nap. This closes the other half: fire ON TIME,
by asking macOS to wake for the next due moment.

Mechanics: a 15-minute planner tick computes the earliest future due
time across every store (reminders, agent tasks, goal cycles, daily
briefs, self-review) and keeps exactly ONE `pmset schedule wake` armed
two minutes ahead of it. Only the NEXT wake is ever scheduled — each
wake's jobs re-plan the one after, so the chain sustains itself and
stale entries never pile up (an extra wake is harmless: the Mac dozes
off again).

pmset needs root. Kyraan never holds a password: it calls
`sudo -n pmset`, which works only after the owner installs a
pmset-scoped rule ONCE:

    echo "$USER ALL=(root) NOPASSWD: /usr/bin/pmset" | \
        sudo tee /etc/sudoers.d/kyraan-pmset

Without the rule the planner logs wake_sudo_unavailable once per boot
and Kyraan stays on late-but-honest delivery — degraded, not broken.
"""
import subprocess
from datetime import datetime, time as _time, timedelta

from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.logging_setup import log_event

_BUFFER_MIN = 2          # wake this long before the due moment
_HORIZON_H = 20          # ignore dues further out — tomorrow's tick owns them
_last_armed: str | None = None
_sudo_warned = False


def _daily_occurrence(at: _time | None, now: datetime) -> datetime | None:
    if at is None:
        return None
    candidate = now.replace(hour=at.hour, minute=at.minute,
                            second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def next_due_time(now: datetime | None = None) -> datetime | None:
    """The earliest future moment any scheduled surface owes a fire.
    Best-effort per source: one broken store must not silence the rest."""
    now = now or local_now()
    dues: list = []

    def _add(iso: str) -> None:
        try:
            when = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return
        if when.tzinfo is None:
            when = when.replace(tzinfo=now.tzinfo)
        if now < when <= now + timedelta(hours=_HORIZON_H):
            dues.append(when)

    try:
        from kyraan.triggers import store
        for r in store.list_pending():
            _add(r.when_iso)
    except Exception as exc:
        log_event("wake_source_error", source="reminders", error=str(exc)[:80])
    try:
        from kyraan.triggers import agent_tasks
        for t in agent_tasks.list_active():
            _add(t.when_iso)
    except Exception as exc:
        log_event("wake_source_error", source="tasks", error=str(exc)[:80])
    try:
        from kyraan.triggers import goals
        for g in [x for x in goals._load() if x.get("status") == "active"]:
            _add(g.get("next_cycle_iso", ""))
    except Exception as exc:
        log_event("wake_source_error", source="goals", error=str(exc)[:80])
    try:
        from kyraan.triggers import briefs, self_review
        for at in (briefs.brief_time("morning"), briefs.brief_time("evening"),
                   self_review.review_time()):
            due = _daily_occurrence(at, now)
            if due:
                dues.append(due)
    except Exception as exc:
        log_event("wake_source_error", source="briefs", error=str(exc)[:80])
    return min(dues) if dues else None


def _pmset(args: list) -> bool:
    """`sudo -n pmset ...` — True on success. Injectable for tests via
    monkeypatch; never prompts (that's the -n)."""
    try:
        proc = subprocess.run(["sudo", "-n", "/usr/bin/pmset"] + args,
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_event("wake_pmset_error", error=str(exc)[:120])
        return False
    if proc.returncode != 0:
        global _sudo_warned
        if not _sudo_warned:
            _sudo_warned = True
            log_event("wake_sudo_unavailable",
                      hint="install /etc/sudoers.d/kyraan-pmset "
                           "(see control_plane/wake.py)")
        return False
    return True


def plan(now: datetime | None = None) -> str | None:
    """One planner pass: arm the next wake if the target moved. Returns
    the armed pmset date-string (for logs/tests) or None."""
    global _last_armed
    now = now or local_now()
    due = next_due_time(now)
    if due is None:
        return None
    wake_at = due - timedelta(minutes=_BUFFER_MIN)
    if wake_at <= now:
        return None  # due imminently — we're awake, the job queue has it
    stamp = wake_at.strftime("%m/%d/%y %H:%M:%S")
    if stamp == _last_armed:
        return _last_armed
    if _pmset(["schedule", "wake", stamp]):
        _last_armed = stamp
        log_event("wake_armed", at=stamp, due=due.isoformat())
        return stamp
    return None


def sudo_ready() -> bool:
    """For the nightly health check: can the planner actually arm wakes?"""
    return _pmset(["-g"])
