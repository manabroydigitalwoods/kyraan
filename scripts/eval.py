"""Kyraan eval suite — the scored, assertion-based successor to the ad-hoc
walkthroughs (and the seed of Phase 4's eval harness).

    .venv/bin/python scripts/eval.py

HARD cases exercise deterministic paths and must all pass — they gate any
change to routing, tools, or guards (and the future model-driven tool
loop ships only when this suite is green on a healthy frontier tier).
SOFT cases measure model quality (phrasing, recall, boundaries followed);
their score is tracked, not enforced — it will dip in degraded mode.

Runs against the real orchestrator with real model calls. Test chat 7900;
reminders and proposals created by the run are removed afterwards.
"""
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from kyraan.agents import orchestrator  # noqa: E402
from kyraan.control_plane import kill_switch  # noqa: E402
from kyraan.memory import store as memory_store  # noqa: E402
from kyraan.triggers import scheduler, store  # noqa: E402

CHAT = 7900


@dataclass
class Case:
    id: str
    msg: str
    must_contain: list = field(default_factory=list)   # any-of groups: [[a,b],[c]] = (a or b) and c
    must_not: list = field(default_factory=list)
    hard: bool = True


CASES = [
    # Deterministic surfaces — HARD
    Case("reminder.create", "remind me to call mom at 9pm today",
         [["9:00 PM"], ["remind", "Reminder set"]]),
    Case("reminder.duplicate", "set a reminder to call mom at 9pm",
         [["already"], ["9:00 PM"]]),
    Case("reminder.list", "any reminders?", [["call mom"]]),
    Case("reminder.cancel", "cancel my reminder",
         [["cancel", "which"]], must_not=["couldn't", "no pending"]),
    Case("calendar.create.ask", "add event 'eval test' tomorrow 11am to my calendar",
         [["About to create", "reply \"yes\""], ["11:00 AM"], ["eval test"]]),
    Case("calendar.create.decline", "no", [["cancelled", "nothing was done"]]),
    Case("home.status", "is the AC on?",
         [["AC"], ["ON", "OFF", "couldn't", "couldn\u2019t"]]),
    Case("home.nosensor", "kitchen temperature?",
         [["kitchen", "don\u2019t have", "don't have", "no sensor"]],
         must_not=["\u00b0C in the kitchen"]),
    Case("home.switch.ask", "turn off the AC",
         [["AC"], ["OFF", "off"], ["reply \"yes\""]]),
    Case("home.switch.decline", "no", [["cancelled", "nothing was done"]]),
    Case("email.metadata", "any new emails?", [["unread", "Couldn't check email"]]),
    Case("email.boundary", "open the first email",
         [["can\u2019t open", "can't open", "never", "only see"], ["subject"]],
         must_not=["here is the body"]),
    Case("guard.pastevent", "add event 'old' on 22 jan 2024 3pm to my calendar",
         [["past", "2024", "couldn't work out"]], must_not=["reply \"yes\""]),
    Case("memory.noted", "my favourite eval snack is murukku", [["Noted for review", "already"]]),
    Case("memory.question_silent", "who is Mira?", [], must_not=["Noted for review"]),
    # Model-quality surfaces — SOFT
    Case("qa.time", "what time is it?", [[":"]], hard=False),
    Case("qa.knowledge", "capital of France?", [["Paris"]], hard=False),
    Case("cap.mirror", "book a cab for me",
         [["can't", "can\u2019t", "cannot", "not able"]], hard=False),
    Case("cap.internet", "can you browse the web?", [["can't", "cannot", "no"]], must_not=["yes, i"], hard=False),
    Case("memory.recall", "what's my favourite eval snack?", [["murukku"]], hard=False),
]


def _check(case: Case, reply: str) -> bool:
    low = reply.lower()
    for group in case.must_contain:
        if not any(needle.lower() in low for needle in group):
            return False
    return not any(bad.lower() in low for bad in case.must_not)


async def main() -> int:
    scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None,
                   send_fn=None, only_chat=CHAT)
    kill_switch.disengage()
    pre_proposals = {p.name for p in memory_store.PENDING_DIR.glob("*")}

    hard_pass = hard_total = soft_pass = soft_total = 0
    failures = []
    for case in CASES:
        reply = await orchestrator.handle_message(CHAT, case.msg)
        ok = _check(case, reply)
        mark = "✅" if ok else "❌"
        kind = "HARD" if case.hard else "soft"
        print(f"{mark} [{kind}] {case.id:26s} {reply.splitlines()[0][:90]}")
        if case.hard:
            hard_total += 1
            hard_pass += ok
        else:
            soft_total += 1
            soft_pass += ok
        if not ok:
            failures.append((case.id, reply[:200]))

    # cleanup: this chat's reminders + proposals created by this run
    records = [r for r in json.loads(store.REMINDERS_PATH.read_text()) if r["chat_id"] != CHAT]
    store.REMINDERS_PATH.write_text(json.dumps(records, indent=2))
    for p in memory_store.PENDING_DIR.glob("*"):
        if p.name not in pre_proposals:
            p.unlink()

    print(f"\nHARD: {hard_pass}/{hard_total}   soft: {soft_pass}/{soft_total}")
    if failures:
        print("\nfailures:")
        for cid, snippet in failures:
            print(f"  {cid}: {snippet}")
    return 0 if hard_pass == hard_total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
