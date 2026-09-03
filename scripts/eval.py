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
import os  # noqa: E402
os.environ.setdefault("KYRAAN_SPEND_BUCKET", "eval")   # eval spend is dev spend, not Kyraan's

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
    # Flag-aware since the local-bodies opt-in (2026-08-26): with
    # KYRAAN_EMAIL_BODIES=local the honest answer is a LOCAL summary
    # ending in the never-left-the-machine line; without it, the denial.
    (Case("email.boundary", "open the first email",
          [["read locally", "never left"]])
     if __import__("os").environ.get("KYRAAN_EMAIL_BODIES", "").strip() == "local"
     else Case("email.boundary", "open the first email",
               [["can\u2019t open", "can't open", "never", "only see"], ["subject"]],
               must_not=["here is the body"])),
    Case("guard.pastevent", "add event 'old' on 22 jan 2024 3pm to my calendar",
         [["past", "2024", "couldn't work out"]], must_not=["reply \"yes\""]),
    Case("memory.noted", "my favourite eval snack is murukku", [["Noted for review", "already"]]),
    Case("memory.question_silent", "who is Mira?", [], must_not=["Noted for review"]),
    # Model-quality surfaces — SOFT
    Case("qa.time", "what time is it?", [[":"]], hard=False),
    Case("qa.knowledge", "capital of France?", [["Paris"]], hard=False),
    Case("cap.mirror", "book a cab for me",
         [["can't", "can\u2019t", "cannot", "not able"]], hard=False),
    # Since web.search (SearXNG) landed, the honest answer is "I can
    # SEARCH (snippets + links) but can't open/browse pages" — the old
    # expectation of a flat "no" became a false failure (2026-08-27).
    Case("cap.internet", "can you browse the web?",
         [["search"], ["can't", "can’t", "cannot", "only", "limited", "snippet"]],
         hard=False),
    Case("memory.recall", "what's my favourite eval snack?", [["murukku"]], hard=False),
    # 2026-08-27: the week's new tool surfaces. Networked tools are SOFT
    # (an upstream outage must not redden the gate); the deterministic
    # faces path is HARD.
    Case("faces.forget.unknown", "forget the face Zephyrina",
         [["No stored face", "no stored face", "aren't", "isn't", "don't have"]], hard=True),
    Case("weather.place", "what's the weather in Kolkata right now?",
         [["°C"], ["kolkata", "Kolkata"]], hard=False),
    Case("routes.distance", "how far is Jalpaiguri from Siliguri by car?",
         [["km"]], hard=False),
    Case("places.nearby", "any hospitals near Siliguri?",
         [["hospital", "Hospital"]], hard=False),
    Case("web.current_fact", "who is the current prime minister of india?",
         [["Modi"]], hard=False),
    # P3.3c: episodic recall — this chat's own backfilled history holds
    # an "any hospitals near Siliguri?" conversation from 2026-08-26.
    Case("recall.episode", "what did we discuss about hospitals near Siliguri before?",
         [["hospital", "Hospital"]], hard=False),
    # P3.6b: the relationship graph — "Wife's name is Ruma" is a live
    # fact whose triple (ruma —wife_of→ owner) the graph carries.
    Case("graph.relation", "how is Ruma related to me?",
         [["wife", "Wife"]], hard=False),
    # P3.3d resurrection gate (HARD): main() seeded a dragonfruit fact +
    # episode and FORGOT the fact through the real cascade before any
    # case ran — nothing in this run's history names it, so any
    # "dragonfruit" in the reply is a resurrection through a store.
    Case("recall.resurrection",
         "what did we discuss about my favourite eval fruit?",
         [], must_not=["dragonfruit", "Dragonfruit"]),
    # P3.1c: the undo command, end to end — create, undo-ask naming the
    # concrete inverse, yes executes it, and the reminder is really gone.
    # HARD: deterministic path (needs the local Postgres container up).
    # "tomorrow at 9am" — a fixed "8:30pm today" made this case fail on
    # any eval run after 8:30 PM local (Kyraan rightly refuses past
    # times and asks for a new one, breaking the whole undo chain).
    Case("undo.create", "remind me to water the plants tomorrow at 9am",
         [["9:00 AM"], ["remind", "Reminder set"]]),
    Case("undo.ask", "undo",
         [["cancel the reminder"], ["water the plants"], ["reply \"yes\""]]),
    Case("undo.confirm", "yes", [["undone"]]),
    Case("undo.gone", "any reminders?", [], must_not=["water the plants"]),
]


_SEED_FACT = "Favourite eval fruit is dragonfruit"
_SEED_EPISODE = ("user: by the way my favourite eval fruit is dragonfruit\n"
                 "assistant: Noted — dragonfruit it is.")


def _purge_eval_reminders() -> None:
    """Remove chat-7900 reminders THROUGH the mirror: post-cutover reads
    come from PG, so a bare file rewrite left ghost rows that made
    reminder.create honestly say 'Already set' (degraded run 1)."""
    records = [r for r in json.loads(store.REMINDERS_PATH.read_text())
               if r["chat_id"] != CHAT] if store.REMINDERS_PATH.exists() else []
    store.REMINDERS_PATH.write_text(json.dumps(records, indent=2))
    from kyraan.store import promises
    promises.mirror_reminders(records)


def _fresh_eval_state() -> None:
    """Each run starts with a FRESH conversation — Redis made session
    state survive processes (P3.4a), so the pre-P3.4 assumption that a
    new eval process means a clean window stopped holding: a prior run's
    replies leaked into prompts (degraded run 1 resurfaced 'dragonfruit'
    from its own failing reply)."""
    _purge_eval_reminders()
    # Eval-artifact proposals from killed/older runs poison the pending
    # block of LOCAL prompts (found live: 'dragonfruit' resurrected from
    # the review queue). Real owner proposals never match these markers.
    for path in memory_store.PENDING_DIR.glob("*.md"):
        text = path.read_text().lower()
        if any(marker in text for marker in
               ("eval snack", "eval fruit", "murukku", "dragonfruit",
                "water the plants")):
            path.unlink()
    orchestrator._history[CHAT].clear()
    orchestrator._summary_backlog[CHAT] = []
    from kyraan.control_plane.filelock import atomic_write_text, locked
    from kyraan.agents import session
    with locked(session._summaries_path()):
        summaries = session._load_summaries()
        if summaries.pop(str(CHAT), None) is not None:
            atomic_write_text(session._summaries_path(),
                              json.dumps(summaries, ensure_ascii=False, indent=1))


def _rig_resurrection_gate() -> None:
    """P3.3d: seed a fact + an old episode about it, verify recall finds
    the episode, then FORGET the fact through the real engine path (file
    → PG mirror → episode sweep). The recall.resurrection case then
    proves neither store resurfaces it. Re-runnable: the fact toggles
    back active and the episode unsuppresses here every run."""
    from kyraan.memory import engine
    from kyraan.store import embed as embed_store
    from kyraan.store import episodes, facts, pg
    entries = engine._load()
    seed = next((e for e in entries
                 if e["target"] == "preferences/eval_fruit.md"), None)
    if seed:
        seed["active"], seed["superseded_by"] = True, None
        engine._save(entries)
        facts.mirror_entries([seed])
        fact_id = seed["id"]
    else:
        fact_id = engine.add_fact(_SEED_FACT, "preferences/eval_fruit.md",
                                  "(eval resurrection rig)")
    vector = json.dumps(embed_store.embed([_SEED_EPISODE])[0])
    with pg.connection() as conn:
        conn.execute(
            """INSERT INTO episode (id, chat_id, day, text, embedding, created_at)
               VALUES (%s, %s, '2026-08-20', %s, %s, '2026-08-20T10:00:00+00:00')
               ON CONFLICT (id) DO UPDATE SET suppressed_by = '{}'""",
            (episodes.episode_uuid(CHAT, "resurrection-rig"), CHAT,
             _SEED_EPISODE, vector))
        conn.commit()
    found = episodes.recall(CHAT, "favourite eval fruit dragonfruit")
    assert any("dragonfruit" in line for line in found), \
        "rig broken: the seeded episode must be findable BEFORE the forget"
    engine.forget([fact_id])  # the real cascade: mirror + sweep
    print("resurrection rig: seeded, verified findable, forgotten+swept")


def _check(case: Case, reply: str) -> bool:
    low = reply.lower()
    for group in case.must_contain:
        if not any(needle.lower() in low for needle in group):
            return False
    return not any(bad.lower() in low for bad in case.must_not)


async def main() -> int:
    # Eval traffic stays out of the production transcript (it was writing
    # dev-chat noise into chat.jsonl) — and events go with it.
    from kyraan.control_plane import logging_setup
    eval_dir = logging_setup.LOG_DIR / "eval"
    eval_dir.mkdir(exist_ok=True)
    logging_setup.CHAT_LOG = eval_dir / "chat.jsonl"
    logging_setup.EVENT_LOG = eval_dir / "events.jsonl"
    logging_setup.TRACE_LOG = eval_dir / "traces.jsonl"

    scheduler.init(schedule_fn=lambda *a, **k: None, cancel_fn=lambda *a, **k: None,
                   send_fn=None, only_chat=CHAT)
    kill_switch.disengage()
    _fresh_eval_state()
    _rig_resurrection_gate()
    pre_proposals = {p.name for p in memory_store.PENDING_DIR.glob("*")}

    hard_pass = hard_total = soft_pass = soft_total = 0
    failures = []
    for case in CASES:
        reply = await orchestrator.handle_message(CHAT, case.msg)
        ok = _check(case, reply)
        if not ok:
            # Retry once: gpt-5.4-nano is mildly nondeterministic and the
            # gate flaked twice in one day on cases that passed on re-run.
            # A genuinely broken case fails twice; stateful sequences that
            # can't survive a re-send simply fail the retry too.
            reply = await orchestrator.handle_message(CHAT, case.msg)
            ok = _check(case, reply)
            if ok:
                print(f"   (passed on retry: {case.id})")
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

    # cleanup: this chat's reminders + proposals created by this run.
    # ONLY proposals traceable to this run's case texts — "everything
    # new since eval start" raced the LIVE bot and deleted a real
    # owner proposal made mid-run (found 2026-08-27 23:05: the owner's
    # turn landed a pending fact while the gate ran; the sweep ate it).
    # Over-keeping is safe: the marker purge in _fresh_eval_state
    # catches any stragglers on the next run.
    _purge_eval_reminders()
    case_texts = {case.msg for case in CASES}
    for p in memory_store.PENDING_DIR.glob("*"):
        if p.name in pre_proposals:
            continue
        try:
            text = p.read_text()
        except OSError:
            continue
        if any(msg in text for msg in case_texts):
            p.unlink()
    try:  # this run's action_log rows (best-effort; PG may be down)
        from kyraan.store import pg
        with pg.connection() as conn:
            conn.execute("DELETE FROM action_log WHERE chat_id = %s", (CHAT,))
            conn.commit()
    except Exception:
        pass

    print(f"\nHARD: {hard_pass}/{hard_total}   soft: {soft_pass}/{soft_total}")
    if failures:
        print("\nfailures:")
        for cid, snippet in failures:
            print(f"  {cid}: {snippet}")
    return 0 if hard_pass == hard_total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
