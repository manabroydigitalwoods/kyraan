"""The exact commands (owner 2026-09-03: "how can we resolve if I forgot
the name of the tool? any suggestion which will be near about what I
want"). Live, the owner typed "index memory", "index note", then "i
forget how to index the obsidian notes" — three near-misses of
"reindex vault" — and the model asked what they meant each time. It
could not suggest the phrase: the deterministic rails are not tools,
so they were nowhere in its menu.

This is the catalogue of those rails, in one place: the phrase, what
it does, and the words a person might reach for instead. suggest()
scores a message against it; the orchestrator offers the nearest
command ("Did you mean ... ? Reply yes") when a short message matched
no rail, and "help" lists them. The capability brief carries a compact
line of the phrases so the model can point to one too.
"""
import re

COMMANDS = [
    {"phrase": "reindex vault",
     "what": "re-index the Obsidian notes into memory (all files, current rules)",
     "words": "index reindex re-index indexing notes note obsidian vault sync refresh memory"},
    {"phrase": "review memory",
     "what": "show the facts waiting for your approval",
     "words": "review pending approve facts memory queue waiting"},
    {"phrase": "consolidate memory",
     "what": "merge and dedupe saved memories",
     "words": "consolidate merge dedupe duplicate duplicates clean memory memories"},
    {"phrase": "health report",
     "what": "system health: models, database, integrations, last 24h anomalies",
     "words": "health status check system doctor diagnostics report ok working"},
    {"phrase": "show last turn",
     "what": "explain what happened in the previous reply (tools, timings)",
     "words": "explain last turn why did trace debug previous reply"},
    {"phrase": "private mode on",
     "what": "keep every turn on this Mac until 'private mode off'",
     "words": "private secret local mode offline confidential"},
    {"phrase": "undo", "suggest": False,
     "what": "reverse the last action (reminder, event, switch, face)",
     "words": "undo revert reverse rollback last action"},
    {"phrase": "forget the face <name>",
     "what": "delete an enrolled face template",
     "words": "forget delete remove face template biometric"},
    {"phrase": "forget the document <name>",
     "what": "delete a saved document or photo memory",
     "words": "forget delete remove document doc photo file memory"},
    {"phrase": "create a person for <name>",
     "what": "add someone to the person registry (links an enrolled face of that name)",
     "words": "create add register person contact people registry face"},
    {"phrase": "what are my medications", "suggest": False,
     "what": "list your saved medicines and supplements (or Kiaan's)",
     "words": "medicines medications meds supplements prescription tablets"},
    {"phrase": "code: <task>",
     "what": "hand a coding task on the kyraan2.0 repo to Claude Code (own branch; you merge)",
     "words": "code coding claude develop implement repo programming"},
    {"phrase": "code status",
     "what": "state of the latest coding task",
     "words": "code coding claude running done progress"},
    {"phrase": "kiaan status",
     "what": "Kiaan's age, vaccinations done and due, milestones to watch",
     "words": "kiaan vaccine vaccination vaccines schedule milestone milestones shot dose"},
    {"phrase": "what's open",
     "what": "unanswered Slack mentions, important unread mail count, slipped reminders",
     "words": "open pending unanswered reply replies missed slipped"},
    {"phrase": "house status",
     "what": "bedroom air and temperature, AC and its energy, purifier mode and filters",
     "words": "house home status filters filter energy purifier ac"},
    {"phrase": "where am i",
     "what": "your last shared location, distance from home, any saved place you're at",
     "words": "where location place position last seen"},
    {"phrase": "remember this place as <name>",
     "what": "save the last shared location as a named place (arrival notes)",
     "words": "remember save place location spot here"},
    {"phrase": "list learned rules",
     "what": "show the behaviour rules Kyraan learned from corrections",
     "words": "list learned rules lessons corrections behaviour"},
    {"phrase": "list tasks", "suggest": False,
     "what": "show scheduled tasks",
     "words": "list tasks scheduled schedule jobs"},
    {"phrase": "list reminders", "suggest": False,
     "what": "show pending reminders",
     "words": "list reminders pending upcoming"},
]

_SYNONYMS = {
    "indexing": "index", "reindex": "index", "re-index": "index", "notes": "note",
    "memories": "memory", "obsidian": "vault", "docs": "document", "documents": "document",
    "medicine": "medicines", "meds": "medicines", "medication": "medicines",
    "medications": "medicines", "photos": "photo", "faces": "face", "rules": "rule",
    "tasks": "task", "reminders": "reminder", "dedup": "dedupe", "deduplicate": "dedupe",
}
_STOP = frozenset("i me my the a an to of for how do can could you please want that this it is "
                  "was forget forgot how's whats what's kyraan there anything something any "
                  "way able possible option command tool feature there's are we does have "
                  "list show get give tell check".split())   # generic verbs never score
_CAPABILITY_Q = re.compile(
    r"^\s*(?:is\s+there|do\s+we\s+have|do\s+you\s+have|can\s+you|could\s+you|"
    r"how\s+(?:do|can|to)|what'?s\s+the\s+(?:command|way)|any\s+way|anything\s+that)\b",
    re.IGNORECASE)


def is_capability_question(text: str) -> bool:
    """"is there anything that can index obsidian notes?" — a question
    about whether a command exists, answered with the command."""
    return bool(_CAPABILITY_Q.match(str(text or "")))


def _words(text: str) -> set:
    out = set()
    for w in re.findall(r"[a-z][a-z'-]*", str(text or "").lower()):
        w = _SYNONYMS.get(w, w)
        if w not in _STOP and len(w) > 1:
            out.add(w)
    return out


def suggest(text: str, min_score: float = 0.6) -> list:
    """[(phrase, what, score)] best first — overlap between the message's
    content words and a command's phrase+words, scored against the
    MESSAGE (a two-word message fully covered scores 1.0)."""
    mine = _words(text)
    if not mine:
        return []
    out = []
    for cmd in COMMANDS:
        if not cmd.get("suggest", True):
            continue      # covered by a tool: the model answers the natural ask
        core = _words(re.sub(r"<[^>]*>", " ", cmd["phrase"]))   # a slot is not a word
        theirs = core | _words(cmd["words"])
        hit = len(mine & theirs)
        if not hit:
            continue
        if len(mine) == 1 and not (mine & core):
            continue      # one loose word ("note") is not a command
        score = hit / len(mine)
        if score >= min_score:
            out.append((cmd["phrase"], cmd["what"], round(score, 2)))
    out.sort(key=lambda x: -x[2])
    return out


def help_text() -> str:
    lines = ["Exact commands I always understand:"]
    for cmd in COMMANDS:
        lines.append(f'• "{cmd["phrase"]}" — {cmd["what"]}')
    lines.append("Everything else in plain words — reminders, calendar, home, photos, "
                 "questions — I work out from the message.")
    return "\n".join(lines)


def brief_line() -> str:
    """One compact line for the model's brief: the phrases exist, and a
    user who forgets one should be TOLD the phrase, not asked what they
    meant."""
    return ("EXACT COMMANDS (deterministic, outside the tool menu — if the user seems "
            "to be reaching for one, tell them the phrase): "
            + "; ".join(f'"{c["phrase"]}"' for c in COMMANDS) + '. "help" lists them.')
