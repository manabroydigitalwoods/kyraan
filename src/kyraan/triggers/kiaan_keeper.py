"""Kiaan's keeper — the first DUTY (owner 2026-09-03: "something is
missing… I'm thinking of Jarvis; he has lots of things to do").

A duty is an area Kyraan is responsible for noticing, not a command
it waits for. This one owns Kiaan's vaccination schedule and his
milestones:

  * the schedule is the IAP (Indian Academy of Pediatrics) table by
    age, anchored on his birth date from memory;
  * what is DONE comes from the vaccination card the owner photographed
    (dated rows), from a "last vaccination day" photo (inferred: the
    doses due at that age), and from the owner saying so ("kiaan got
    the MR shot today");
  * every morning it checks: a dose due within three weeks gets ONE
    nudge, and one more three days before the window opens; an overdue
    dose is named once a fortnight; a milestone window he has entered
    gets one gentle question if no note or photo mentions it;
  * "kiaan status" answers on demand: age, done, next, milestones.

Everything goes through the same proactive gate as reminders (kill
switch + DND) and the same delivery truth. State is one JSON file.
"""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.filelock import atomic_write_text, locked
from kyraan.control_plane.logging_setup import log_event

PERSON = "kiaan"
STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "duties" / "kiaan_keeper.json"

# (id, label, due from age in months, window closes months later, card keywords)
SCHEDULE = [
    ("bcg", "BCG", 0, 1, ("bcg",)),
    ("opv0", "OPV zero dose", 0, 1, ("o.p.v", "opv")),
    ("hepb1", "Hepatitis B — 1st", 0, 1, ("hepatitis-b", "hepatitis b")),
    ("penta1", "Pentavalent (DTP+Hib+HepB) — 1st", 1.5, 1, ("combination", "triple antigen")),
    ("ipv1", "IPV — 1st", 1.5, 1, ("i.p.v", "ipv")),
    ("rota1", "Rotavirus — 1st", 1.5, 1, ("rotavirus",)),
    ("pcv1", "Pneumococcal (PCV) — 1st", 1.5, 1, ("pneumococcal",)),
    ("penta2", "Pentavalent — 2nd", 2.5, 1, ("combination",)),
    ("ipv2", "IPV — 2nd", 2.5, 1, ("i.p.v", "ipv")),
    ("rota2", "Rotavirus — 2nd", 2.5, 1, ("rotavirus",)),
    ("pcv2", "Pneumococcal (PCV) — 2nd", 2.5, 1, ("pneumococcal",)),
    ("penta3", "Pentavalent — 3rd", 3.5, 1, ("combination",)),
    ("ipv3", "IPV — 3rd", 3.5, 1, ("i.p.v", "ipv")),
    ("rota3", "Rotavirus — 3rd", 3.5, 1, ("rotavirus",)),
    ("pcv3", "Pneumococcal (PCV) — 3rd", 3.5, 1, ("pneumococcal",)),
    ("flu1", "Influenza — 1st", 6, 1, ("influenza",)),
    ("flu2", "Influenza — 2nd (a month after the 1st)", 7, 1, ("influenza",)),
    ("tcv", "Typhoid conjugate (TCV)", 6, 3, ("typhoid",)),
    ("mr1", "Measles-Rubella (MR) — 1st", 9, 1, ("measles", "mmr")),
    ("je1", "Japanese Encephalitis — 1st", 9, 1, ("j.e", "je")),
    ("hepa1", "Hepatitis A — 1st", 12, 3, ("hepatitis-a", "hepatitis a")),
    ("je2", "Japanese Encephalitis — 2nd", 12, 2, ("j.e", "je")),
    ("pcvb", "Pneumococcal booster", 12, 3, ("pneumococcal",)),
    ("mmr2", "MMR — 2nd", 15, 3, ("mmr",)),
    ("var1", "Varicella (chicken pox) — 1st", 15, 3, ("varicella",)),
    ("dtpb1", "DTP booster — 1st", 16, 2, ("triple antigen", "combination")),
    ("hibb", "Hib booster", 16, 2, ("hib",)),
    ("ipvb", "IPV booster", 16, 2, ("i.p.v", "ipv")),
    ("hepa2", "Hepatitis A — 2nd", 18, 6, ("hepatitis-a", "hepatitis a")),
    ("tcvb", "Typhoid booster", 24, 6, ("typhoid",)),
    ("flu_y", "Influenza — yearly", 18, 12, ("influenza",)),
]

MILESTONES = [
    ("sit", "sitting without support", 6, 9),
    ("crawl", "crawling", 7, 10),
    ("stand", "standing with support", 8, 11),
    ("standalone", "standing on his own", 9, 13),
    ("words", "first words (mama / dada)", 9, 14),
    ("steps", "first steps", 11, 15),
    ("wave", "waving bye-bye", 9, 13),
    ("run", "running", 15, 20),
    ("twowords", "two-word phrases", 18, 26),
]

NUDGE_AHEAD_DAYS = 21
REMIND_AHEAD_DAYS = 3
OVERDUE_REPEAT_DAYS = 14


def check_time():
    """config duties.kiaan_keeper: {enabled, time} — default 08:05, on."""
    from datetime import time as _time
    from kyraan.control_plane import config
    cfg = ((config.load().get("duties") or {}).get("kiaan_keeper") or {})
    if cfg.get("enabled", True) is False:
        return None
    hh, mm = str(cfg.get("time", "08:05")).split(":")
    return _time(int(hh), int(mm))


# ---------------------------------------------------------------- state --

def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"done": {}, "nudged": {}, "asked": {}}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- facts --

def birth_date() -> date | None:
    """From the owner-reviewed fact ("My son Kiaan was born on 12-10-2025")."""
    try:
        from kyraan.store import pg
        with pg.connection() as conn:
            rows = conn.execute(
                "SELECT content FROM fact WHERE active AND content ILIKE %s",
                ("%kiaan%born%",)).fetchall()
    except Exception:
        return None
    for (content,) in rows:
        m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", content)
        if m:
            d, mo, y = (int(x) for x in m.groups())
            try:
                return date(y, mo, d)
            except ValueError:
                continue
    return None


def age_months(born: date, on: date | None = None) -> float:
    on = on or local_now().date()
    return (on - born).days / 30.4375


def card_text() -> str:
    try:
        from kyraan.store import pg
        with pg.connection() as conn:
            rows = conn.execute(
                """SELECT text FROM document WHERE suppressed_by = '{}'
                   AND %s = ANY(subject_persons) AND (caption ILIKE '%%vaccin%%' OR text ILIKE '%%immunis%%')
                   ORDER BY created_at DESC LIMIT 3""", (PERSON,)).fetchall()
        return "\n".join(r[0] for r in rows)
    except Exception:
        return ""


def visit_dates() -> list:
    """Dates of captures the owner labelled as vaccination days."""
    try:
        from kyraan.store import pg
        with pg.connection() as conn:
            rows = conn.execute(
                """SELECT created_at::date FROM document WHERE suppressed_by = '{}'
                   AND %s = ANY(subject_persons) AND kind = 'moment'
                   AND caption ILIKE '%%vaccin%%' ORDER BY created_at""", (PERSON,)).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


_DATE = re.compile(r"(\d{1,2})[/\-. ]+([A-Za-z]{3,9}|\d{1,2})[/\-. ]+(\d{2,4})")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _dates_in(text: str) -> list:
    out = []
    for d, mo, y in _DATE.findall(text):
        try:
            month = int(mo) if mo.isdigit() else _MONTHS.get(mo[:3].lower())
            year = int(y) if len(y) == 4 else 2000 + int(y)
            if month:
                out.append(date(year, month, int(d)))
        except ValueError:
            continue
    return out


def done_from_card(text: str) -> dict:
    """{schedule id: date} for doses whose card row carries a date. The
    card lists rows by vaccine with a dose column; the n-th date under a
    vaccine's block is that vaccine's n-th dose."""
    done: dict = {}
    if not text:
        return done
    # a numbered VACCINE heading starts a block; a numbered DOSE line
    # ("1. First | 30 NOV 2025") inside it does not
    blocks = re.split(r"\n\s*\d{1,2}\.\s+(?!(?:First|Second|Third|Fourth|Booster|1st|2nd|3rd)\b)",
                      "\n" + text)
    for block in blocks:
        head = block.strip().lower()[:60]
        dates = _dates_in(block)
        if not dates:
            continue
        seq = [s for s in SCHEDULE if any(k in head for k in s[4])]
        for n, when in enumerate(sorted(dates)):
            if n < len(seq):
                done.setdefault(seq[n][0], when.isoformat())
    return done


def done_map(state: dict | None = None) -> dict:
    """id -> {"date", "source"}; owner statements beat the card, which
    beats inference from a vaccination-day photo."""
    state = state or _load()
    out = {}
    for sid, when in done_from_card(card_text()).items():
        out[sid] = {"date": when, "source": "card"}
    born = birth_date()
    for visit in visit_dates():
        if born is None:
            break
        months = age_months(born, visit)
        for sid, label, due, window, _k in SCHEDULE:
            if sid in out:
                continue
            if due - 0.75 <= months <= due + window + 1.0:
                out[sid] = {"date": visit.isoformat(), "source": "photo (inferred)"}
    for sid, rec in (state.get("done") or {}).items():
        out[sid] = {"date": rec.get("date", ""),
                    "source": "skipped (your call)" if rec.get("skipped") else "you said"}
    return out


# ------------------------------------------------------------- reasoning --

def upcoming(born: date, done: dict, today: date | None = None) -> list:
    """[(id, label, due_date, status)] — status in due-soon / overdue /
    later; nothing already done."""
    today = today or local_now().date()
    out = []
    for sid, label, due, window, _k in SCHEDULE:
        if sid in done:
            continue
        due_date = born + timedelta(days=round(due * 30.4375))
        close = due_date + timedelta(days=round(window * 30.4375))
        if close < today - timedelta(days=90):
            continue          # long past and never recorded: not a nudge, a question for status
        if today > close:
            status = "overdue"
        elif due_date - today <= timedelta(days=NUDGE_AHEAD_DAYS):
            status = "due-soon"
        else:
            status = "later"
        out.append((sid, label, due_date, status))
    out.sort(key=lambda x: x[2])
    return out


def milestone_windows(born: date, today: date | None = None) -> list:
    today = today or local_now().date()
    m = age_months(born, today)
    return [(mid, label) for mid, label, lo, hi in MILESTONES if lo <= m <= hi]


def milestone_recorded(label: str) -> bool:
    """A note or photo already mentions it (by its key words)."""
    key = re.findall(r"[a-z]{4,}", label.lower())[:2]
    if not key:
        return False
    try:
        from kyraan.store import pg
        with pg.connection() as conn:
            docs = conn.execute(
                """SELECT count(*) FROM document WHERE suppressed_by = '{}'
                   AND %s = ANY(subject_persons) AND text ILIKE %s""",
                (PERSON, f"%{key[0]}%")).fetchone()
            facts = conn.execute(
                "SELECT count(*) FROM fact WHERE active AND content ILIKE %s AND content ILIKE %s",
                (f"%{PERSON}%", f"%{key[0]}%")).fetchone()
        return bool((docs and docs[0]) or (facts and facts[0]))
    except Exception:
        return False


def status_text() -> str:
    born = birth_date()
    if born is None:
        return ("I don't have Kiaan's birth date in memory yet — tell me \"Kiaan was born on "
                "12 October 2025\" and I'll keep his vaccination schedule.")
    today = local_now().date()
    months = age_months(born, today)
    done = done_map()
    lines = [f"Kiaan is {int(months)} months old (born {born.strftime('%d %b %Y')})."]
    recent = sorted(done.items(), key=lambda kv: kv[1]["date"])[-4:]
    if recent:
        lines.append("Done: " + "; ".join(
            f"{next(s[1] for s in SCHEDULE if s[0] == sid)} — {rec['date']} ({rec['source']})"
            for sid, rec in recent))
    nxt = upcoming(born, done, today)
    soon = [x for x in nxt if x[3] in ("due-soon", "overdue")][:4]
    later = [x for x in nxt if x[3] == "later"][:3]
    if soon:
        lines.append("Due: " + "; ".join(
            f"{label} — {due.strftime('%d %b')}{' (overdue)' if st == 'overdue' else ''}"
            for _s, label, due, st in soon))
    if later:
        lines.append("Next: " + "; ".join(f"{label} — {due.strftime('%d %b %Y')}" for _s, label, due, _st in later))
    ms = [label for _m, label in milestone_windows(born, today) if not milestone_recorded(label)]
    if ms:
        lines.append("Milestones to watch for now: " + ", ".join(ms) + ".")
    lines.append('Tell me "kiaan got the MR shot today" or "kiaan started walking" and I\'ll record it.')
    return "\n".join(lines)


def mark_done(vaccine_words: str, when: date | None = None, skipped: bool = False) -> list:
    """Owner says a dose was given (or, skipped=True, that the doctor is
    not giving it): match by keywords, record, return labels."""
    when = when or local_now().date()
    low = vaccine_words.lower()
    state = _load()
    done = done_map(state)
    hits = []
    words = set(re.findall(r"[a-z0-9]+", low.replace(".", "")))
    for sid, label, _d, _w, keys in SCHEDULE:
        if sid in done:
            continue
        # keys ("measles", "hepatitis-a"), the label's words, and its
        # abbreviation in brackets ("(MR)", "(PCV)") all name the dose
        names = {k.replace(".", "").replace("-", " ") for k in keys}
        names |= set(re.findall(r"[a-z]{2,}", label.split(" —")[0].lower().replace("-", " ")))
        names |= set(re.findall(r"\(([a-z]+)\)", label.lower()))
        names -= {"st", "nd", "rd", "th", "the", "and", "after", "month", "dose", "booster", "zero"}
        if any(n in low.replace(".", "") if " " in n else n in words for n in names):
            hits.append((sid, label))
    hits = hits[:1]   # the next undone dose of that vaccine
    for sid, label in hits:
        state.setdefault("done", {})[sid] = {"date": when.isoformat(), "skipped": skipped}
    if hits:
        _save(state)
        log_event("kiaan_keeper_marked", doses=[h[0] for h in hits], date=when.isoformat())
    return [label for _s, label in hits]


# ------------------------------------------------------------------ duty --

async def fire(chat_id: int, send_fn) -> bool:
    """The morning check. Sends at most ONE message, only when there is
    something new to say; delivery truth decides what is remembered."""
    born = birth_date()
    if born is None:
        return False
    if not kernel.can_send_proactively(chat_id=chat_id):
        return False
    today = local_now().date()
    state = _load()
    done = done_map(state)
    nudged = state.setdefault("nudged", {})
    asked = state.setdefault("asked", {})
    lines = []
    for sid, label, due, st in upcoming(born, done, today):
        key_first = f"{sid}:first"
        key_near = f"{sid}:near"
        key_over = f"{sid}:overdue"
        if st == "due-soon" and key_first not in nudged:
            lines.append(f"• {label} is due around {due.strftime('%d %b')} — worth booking the clinic.")
            nudged[key_first] = today.isoformat()
        elif st == "due-soon" and 0 <= (due - today).days <= REMIND_AHEAD_DAYS and key_near not in nudged:
            lines.append(f"• {label} — {due.strftime('%A %d %b')}, in {(due - today).days} day(s).")
            nudged[key_near] = today.isoformat()
        elif st == "overdue":
            last = nudged.get(key_over)
            if not last or (today - date.fromisoformat(last)).days >= OVERDUE_REPEAT_DAYS:
                lines.append(f"• {label} was due {due.strftime('%d %b')} and I have no record of it — "
                             "done already? Tell me and I'll note it.")
                nudged[key_over] = today.isoformat()
    last_ask = max(asked.values(), default="")
    if not last_ask or (today - date.fromisoformat(last_ask)).days >= 7:
        for mid, label in milestone_windows(born, today):
            if mid in asked or milestone_recorded(label):
                continue
            lines.append(f"• He's at the age for {label} — has it happened? A photo or a line and I'll keep it.")
            asked[mid] = today.isoformat()
            break   # one milestone question a WEEK — a duty, not a nag
    if not lines:
        _save(state)
        return False
    text = "👶 Kiaan — " + f"{int(age_months(born, today))} months\n" + "\n".join(lines)
    ok = await send_fn(chat_id, text)
    if ok is False:
        return False   # not delivered: leave the state so it is said again
    _save(state)
    log_event("kiaan_keeper_sent", chat_id=chat_id, items=len(lines))
    return True
