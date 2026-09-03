"""Chief of staff — duty #3 (owner 2026-09-03, "go").

What it owns: the shape of the working day. Three moments:

  * MORNING (inside the 07:30 brief): a "Needs a reply" section —
    Slack mentions surfaced and still unanswered, how many important
    unread mails (a COUNT: sender/subject metadata never enters the
    cloud-visible history, §3a), reminders that slipped.
  * 18:00 "still open": only what did not get closed during the day —
    mentions never answered, reminders overdue. Silent when everything
    is done. Weekdays only.
  * EVENING (inside the evening brief): meeting prep — for tomorrow's
    events that name a person Kyraan knows, a few substantive lines
    from their people note and when they were last seen.

"What's open?" answers on demand. Same proactive gate, same delivery
truth, one state file for what was already said.
"""
import json
import re
from datetime import datetime, time, timedelta
from pathlib import Path

from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.filelock import atomic_write_text
from kyraan.control_plane.logging_setup import log_event

STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "duties" / "chief_of_staff.json"


def _cfg() -> dict:
    from kyraan.control_plane import config
    return (config.load().get("duties") or {}).get("chief_of_staff") or {}


def still_open_time() -> time | None:
    cfg = _cfg()
    if cfg.get("enabled", True) is False:
        return None
    hh, mm = str(cfg.get("still_open", "18:00")).split(":")
    return time(int(hh), int(mm))


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"said": {}}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False))


# ------------------------------------------------------------ the items --

async def open_mentions() -> list:
    """Slack mentions surfaced today and not answered since — answered
    means the owner (or Kyraan on their behalf) posted in that channel
    after the mention."""
    try:
        from kyraan.triggers import slack_watch
    except Exception:
        return []
    state = slack_watch._load()
    today = local_now().date().isoformat()
    ours = set(state.get("kyraan_posted", []))
    out = []
    by_channel: dict = {}
    for m in state.get("open_mentions", []):
        if str(m.get("surfaced_at", ""))[:10] != today:
            continue
        by_channel.setdefault(m["channel"], []).append(m)
    for channel, mentions in by_channel.items():
        try:
            raw = await kernel.run_tool(kernel.ToolCall(
                "slack.history", {"channel_id": channel, "limit": "1d"}), meta=True)
            rows = slack_watch.parse_history(raw)
        except Exception:
            rows = []
        for m in mentions:
            answered = any(
                r["ts"] > m["ts"] and (r["user_id"] == slack_watch._owner_user_id or r["text"] in ours)
                for r in rows)
            if not answered:
                out.append(m)
    return out


def overdue_reminders(chat_id: int) -> list:
    from kyraan.triggers import scheduler, store
    now = local_now()
    out = []
    for r in store.list_pending(chat_id):
        try:
            when = scheduler._parse_when(r.when_iso)
        except ValueError:
            continue
        if when < now - timedelta(minutes=30) and not r.repeat:
            out.append((when, r.text))
    return sorted(out)


async def important_mail_count() -> int | None:
    try:
        from kyraan.tools import gmail
        if not gmail.configured():
            return None
        result = await kernel.run_tool(kernel.ToolCall("email.important", {"limit": 10}), meta=True)
        return len((result or {}).get("messages", []))
    except Exception:
        return None


async def needs_reply_lines(chat_id: int) -> list:
    """The morning section — one line per open thing, no email metadata."""
    lines = []
    for m in await open_mentions():
        lines.append(f'- Slack {m["channel"]}: {m["user"]} — "{m["question"][:80]}"')
    n = await important_mail_count()
    if n:
        lines.append(f"- {n} important unread mail{'s' if n != 1 else ''} (say \"important emails\")")
    for when, text in overdue_reminders(chat_id)[:5]:
        lines.append(f"- Slipped: {text} (was {when.strftime('%I:%M %p').lstrip('0')})")
    return lines


# -------------------------------------------------------- meeting prep --

def _people_in(title: str) -> list:
    """Registry persons named in an event title, by whole word."""
    try:
        from kyraan.store import persons
        nm = persons.name_map()
    except Exception:
        return []
    low = title.lower()
    found = []
    for name, pid in nm.items():
        if pid == "owner" or pid in found or len(name) < 3:
            continue
        if re.search(rf"\b{re.escape(name)}\b", low):
            found.append(pid)
    return found


def _note_lines(pid: str, limit: int = 3) -> list:
    """Substantive lines from the person's note (template blanks skipped)
    and the date of the last capture with them."""
    from kyraan.store import pg
    from kyraan.store.notes import substantive
    lines = []
    try:
        with pg.connection() as conn:
            row = conn.execute(
                """SELECT text FROM document WHERE kind = 'note' AND suppressed_by = '{}'
                   AND %s = ANY(subject_persons) AND exposure = 'cloud_ok'
                   ORDER BY updated_at DESC LIMIT 1""", (pid,)).fetchone()
            last = conn.execute(
                """SELECT max(created_at)::date FROM document WHERE suppressed_by = '{}'
                   AND kind IN ('moment', 'photo') AND %s = ANY(subject_persons)""", (pid,)).fetchone()
    except Exception:
        return lines
    if row:
        for ln in row[0].splitlines():
            ln = ln.strip()
            if ln.startswith("#") or not ln or not substantive(ln):
                continue
            lines.append(ln.lstrip("- ")[:120])
            if len(lines) >= limit:
                break
    if last and last[0]:
        lines.append(f"last photo together: {last[0].strftime('%d %b %Y')}")
    return lines


def prep_lines(events: list) -> list:
    """For tomorrow's events that name someone Kyraan knows."""
    out = []
    for e in events or []:
        for pid in _people_in(e.get("title", "")):
            notes = _note_lines(pid)
            if not notes:
                continue
            out.append(f"About {pid.replace('_', ' ').title()} ({e.get('title', '')[:40]}):")
            out += [f"  · {n}" for n in notes]
    return out


# ------------------------------------------------------------ the duty --

async def status_text(chat_id: int) -> str:
    lines = await needs_reply_lines(chat_id)
    if not lines:
        return "Nothing open — no unanswered Slack mentions, no slipped reminders."
    return "Open right now:\n" + "\n".join(lines)


async def fire_still_open(chat_id: int, send_fn) -> bool:
    """18:00 on a weekday: say only what is still open; silent otherwise."""
    if local_now().weekday() >= 5:
        return False
    if not kernel.can_send_proactively(chat_id=chat_id):
        return False
    lines = await needs_reply_lines(chat_id)
    if not lines:
        return False
    state = _load()
    key = local_now().date().isoformat()
    digest = "\n".join(lines)
    if state.get("said", {}).get(key) == digest:
        return False              # already said today, nothing changed
    text = "🗂 Still open today:\n" + digest
    if await send_fn(chat_id, text) is False:
        return False
    state.setdefault("said", {})[key] = digest
    state["said"] = {k: v for k, v in state["said"].items() if k >= (local_now().date() - timedelta(days=7)).isoformat()}
    _save(state)
    log_event("chief_of_staff_sent", chat_id=chat_id, items=len(lines))
    return True
