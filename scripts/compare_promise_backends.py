"""P3.2d parity harness: the promise stores (reminders, agent tasks,
cost ledger) — file state vs Postgres mirror, field-for-field.

    scripts/compare_promise_backends.py             # diff now
    scripts/compare_promise_backends.py --sync      # seed pg from files, then diff
    scripts/compare_promise_backends.py --exercise  # scripted mutation
        sequence on a scratch chat, diffing after EVERY mutation

Exit 0 = parity clean. The P3.2c-style soak counts clean days of the
default diff.
"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.model_router import router  # noqa: E402
from kyraan.store import promises  # noqa: E402
from kyraan.triggers import agent_tasks, store  # noqa: E402

_SCRATCH_CHAT = -424242


def _norm(records, fields):
    return sorted(
        ({f: r.get(f, promises._DEFAULTS.get(f)) for f in fields} for r in records),
        key=lambda r: str(r["id"]))


def diff_all(label: str = "") -> int:
    problems = []
    pairs = [
        ("reminders", store._load_all(), promises.load_reminders(),
         promises._REMINDER_FIELDS),
        ("tasks", agent_tasks._load(), promises.load_tasks(),
         promises._TASK_FIELDS),
    ]
    for name, file_records, pg_records, fields in pairs:
        if pg_records is None:
            problems.append(f"{name}: pg unreadable")
            continue
        f_norm, p_norm = _norm(file_records, fields), _norm(pg_records, fields)
        if f_norm != p_norm:
            problems.append(f"{name}: file={json.dumps(f_norm, default=str)[:400]} "
                            f"pg={json.dumps(p_norm, default=str)[:400]}")
    file_ledger = router._read_ledger_file()
    pg_ledger = promises.load_ledger()
    if pg_ledger is None:
        problems.append("ledger: pg unreadable")
    elif file_ledger != pg_ledger:
        only_f = {k for k in file_ledger if file_ledger.get(k) != pg_ledger.get(k)}
        only_p = {k for k in pg_ledger if pg_ledger.get(k) != file_ledger.get(k)}
        problems.append(f"ledger keys differing: {sorted(only_f | only_p)}")
    tag = f" [{label}]" if label else ""
    if problems:
        print(f"❌ PARITY BROKEN{tag}:")
        for p in problems:
            print(f"   {p}")
        return 1
    print(f"✅ parity clean{tag}: {len(store._load_all())} reminders, "
          f"{len(agent_tasks._load())} tasks, {len(router._read_ledger_file())} ledger keys")
    return 0


def seed() -> None:
    ok = (promises.mirror_reminders(store._load_all())
          and promises.mirror_tasks(agent_tasks._load())
          and promises.mirror_ledger(router._read_ledger_file()))
    print("seeded pg from files" if ok else "seed FAILED (see events.jsonl)")


def exercise() -> int:
    """Every mutation class, a diff after each, scratch data removed."""
    bad = 0
    r = store.add(_SCRATCH_CHAT, "parity probe", "2027-01-01T09:00:00+05:30",
                  repeat="daily")
    bad += diff_all("add")
    store.claim_for_send(r.id)
    bad += diff_all("claim")
    store.roll_forward(r.id, "2027-01-02T09:00:00+05:30")
    bad += diff_all("roll_forward")
    store.mark_sent(r.id)
    bad += diff_all("mark_sent")
    store.cancel(r.id)
    bad += diff_all("cancel")
    agent_tasks._schedule_fn = agent_tasks._schedule_fn or (lambda *a, **k: None)
    t = agent_tasks.create(_SCRATCH_CHAT, "parity probe task",
                           "2027-01-01T20:00:00+05:30")
    bad += diff_all("task create")
    agent_tasks._set_pending_result(t.id, "probe result")
    bad += diff_all("task pending_result")
    agent_tasks.cancel(t.id)
    bad += diff_all("task cancel")
    return bad


def main() -> int:
    if "--sync" in sys.argv:
        seed()
        return diff_all("after sync")
    if "--exercise" in sys.argv:
        return 1 if exercise() else 0
    return diff_all()


if __name__ == "__main__":
    sys.exit(main())
