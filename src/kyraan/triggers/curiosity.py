"""Curiosity queue — the last Phase 4 item (plan §Phase 4: "batched,
rate-limited proactive questions").

Doctrine-shaped: candidates are gathered DETERMINISTICALLY from real
knowledge gaps — no model ever invents a question — capped at ONE per
day, delivered inside the morning brief (so it is batched, DND-safe by
construction, and never a new interruption), and a 14-day ask memory
stops repeats. The owner's natural reply flows through the normal loop
→ extraction → review path; curiosity itself writes nothing.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.filelock import atomic_write_text, locked
from kyraan.control_plane.logging_setup import log_event

STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "curiosity.json"
_REASK_DAYS = 14


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"asked": {}}


def _mark_asked(key: str) -> None:
    with locked(STATE_PATH):
        state = _state()
        state.setdefault("asked", {})[key] = local_now().date().isoformat()
        atomic_write_text(STATE_PATH, json.dumps(state, indent=1))


def _recently_asked(key: str, asked: dict) -> bool:
    when = asked.get(key)
    if not when:
        return False
    try:
        return (local_now().date()
                - datetime.fromisoformat(when).date()).days < _REASK_DAYS
    except ValueError:
        return False


def collect_candidates(chat_id: int) -> list:
    """(key, question) pairs from REAL knowledge gaps, best-first. Every
    source is a deterministic read; a failure just drops that source."""
    out = []
    try:
        from kyraan.agents import faces
        from kyraan.store import persons
        registry = {p[0] for p in persons.list_persons()}
        enrolled = {n.lower().replace(" ", "_").replace("-", "_")
                    for n in (faces.enrolled_names()
                              if faces.available() else [])}
        # A face Kyraan recognizes but cannot link to anything.
        for slug in sorted(enrolled - registry):
            name = slug.replace("_", " ").title()
            out.append((f"face_unregistered:{slug}",
                        f"I can recognize {name} in photos, but they're "
                        "not in the people I track — say "
                        f'"add {name} as a person" if documents and facts '
                        "should link to them."))
    except Exception:
        pass
    try:
        from kyraan.memory import engine
        from kyraan.store import documents, persons
        import re
        entries = engine.active_entries()
        for pid, *_ in persons.list_persons():
            if pid == "owner":
                continue
            names = [n for n, p in persons.name_map().items() if p == pid]
            pattern = re.compile(
                r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b",
                re.IGNORECASE)
            known = any(pattern.search(e["content"]) for e in entries)
            if not known:
                display = pid.replace("_", " ").title()
                out.append((f"person_blank:{pid}",
                            f"You track {display}, but I have no approved "
                            "facts about them — who are they to you?"))
    except Exception:
        pass
    try:
        from kyraan.memory import engine
        disputed = [e for e in engine.active_entries()
                    if "disputed" in (e.get("flags") or [])]
        if disputed:
            out.append(("disputes_open",
                        f"{len(disputed)} saved fact(s) are marked disputed "
                        "— worth a look when you have a minute."))
    except Exception:
        pass
    try:
        from kyraan.memory import store as memory_store
        pending = len(list(memory_store.PENDING_DIR.glob("*.md")))
        if pending >= 5:
            out.append(("review_backlog",
                        f"{pending} facts are waiting in your review queue "
                        '— say "review memory" to clear them.'))
    except Exception:
        pass
    return out


def daily_line(chat_id: int) -> str | None:
    """At most ONE question, chosen deterministically, never repeated
    within 14 days. Returns the brief line or None (quiet days are
    normal and good). Marked asked at COMPOSE time — if the brief's
    delivery then fails, the retry in _deliver is the recovery; a
    question lost to a double delivery failure resurfaces in 14 days,
    which is the right price for never nagging twice in one morning."""
    asked = _state().get("asked", {})
    for key, question in collect_candidates(chat_id):
        if _recently_asked(key, asked):
            continue
        _mark_asked(key)
        log_event("curiosity_asked", key=key)
        return f"🤔 {question}"
    return None
