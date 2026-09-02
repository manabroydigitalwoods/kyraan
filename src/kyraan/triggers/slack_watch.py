"""Slack mention watch (owner decisions 2026-09-02): every two minutes,
read the watched channels' recent messages, and when someone @mentions
the owner, DRAFT a reply with the read-only loop and hand it to the
owner as a one-tap confirm in Telegram. Nothing is ever posted without
the owner's yes — the first third-party write surface keeps the
standing confirm doctrine intact.

Deterministic mechanics: a per-channel watermark (message ts) advances
only past messages that were actually surfaced (delivery truth); the
first run sets watermarks to "now" so history is never replayed; the
owner's own messages never trigger; mention text enters the draft
prompt as untrusted third-party text (the run is read-only by
construction — the draft is words, the post is the owner's).
"""
import csv
import io
import json
from pathlib import Path

from kyraan.control_plane import kernel
from kyraan.control_plane.filelock import atomic_write_text
from kyraan.control_plane.logging_setup import log_event

STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "slack_watch.json"

_owner_user_id: str = ""
_draft_fn = None     # async (instruction) -> str
_ask_fn = None       # async (chat_id, channel, draft, context) -> None


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"watermarks": {}}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    atomic_write_text(STATE_PATH, json.dumps(state, indent=1))


def note_posted(text: str) -> None:
    """Remember what Kyraan itself posted under the owner's name — those
    lines must never feed back as the owner's voice sample (live
    2026-09-02: each stiff draft made the next one stiffer)."""
    state = _load()
    posted = state.setdefault("kyraan_posted", [])
    if text and text not in posted:
        posted.append(text)
        state["kyraan_posted"] = posted[-50:]
        _save(state)


def parse_history(csv_text: str) -> list:
    """Rows from the server's CSV: [{ts, user_id, user, text, channel}]."""
    rows = []
    try:
        reader = csv.DictReader(io.StringIO(str(csv_text or "")))
        for r in reader:
            if not r.get("MsgID"):
                continue
            rows.append({"ts": r.get("MsgID", ""), "user_id": r.get("UserID", ""),
                         "user": r.get("RealName") or r.get("UserName") or "?",
                         "text": r.get("Text", ""),
                         "channel": r.get("Channel", "")})
    except csv.Error:
        return []
    return rows


def mentions_owner(text: str, owner_id: str, owner_handle: str = "") -> bool:
    low = str(text or "").lower()
    if owner_id and f"<@{owner_id.lower()}>" in low:
        return True
    return bool(owner_handle) and f"@{owner_handle.lower()}" in low


def init(owner_user_id: str, draft_fn, ask_fn, owner_handle: str = "") -> None:
    global _owner_user_id, _draft_fn, _ask_fn, _owner_handle
    _owner_user_id, _draft_fn, _ask_fn = owner_user_id, draft_fn, ask_fn
    _owner_handle = owner_handle


_owner_handle = ""


_META_RE = None


def looks_like_meta_talk(draft: str, owner_name: str = "Maan") -> bool:
    """A draft that talks to the OWNER instead of the sender is not a
    reply — live 2026-09-02: "I can draft it, but I need 1 detail: what
    do you want me to say back to Ruma…" was posted to Ruma."""
    import re
    global _META_RE
    if _META_RE is None:
        _META_RE = re.compile(
            r"\b(?:i can draft|what (?:do|would) you want me to say|do you want "
            r"me to|should i (?:say|reply|post)|proposed reply|reply \"?yes\"?"
            r"|say back to|which plan are you referring)\b|^\s*" + re.escape(owner_name) + r"\b",
            re.IGNORECASE)
    return bool(_META_RE.search(draft or ""))


def _shingles(text: str, n: int = 5) -> set:
    import re
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def repeats_earlier(draft: str, posted: list) -> bool:
    """Deterministic repetition gate (live 2026-09-02: "Today im very
    upset" drew the vaccination-card paragraph a third time). A draft
    sharing any 5-word run with something Kyraan already posted is a
    repeat — retry, then surface draftless."""
    mine = _shingles(draft)
    return any(mine & _shingles(p) for p in posted or [])


def out_of_proportion(draft: str, question: str) -> bool:
    """A four-word message does not earn a paragraph: short messages
    get short replies unless they actually ask for information."""
    q = str(question or "")
    asks = "?" in q or any(w in q.lower().split() for w in
                           ("what", "when", "which", "where", "how", "plan",
                            "date", "time", "any"))
    if asks:
        return len(draft) > 600
    return len(draft) > max(160, 3 * len(q) + 80)


def is_informational(question: str) -> bool:
    """Only an actual question earns fact/document retrieval — feeding
    a vaccination card into "I'm upset" is how the card came back."""
    q = str(question or "").lower()
    return "?" in q or any(w in q.split() for w in
                           ("what", "when", "which", "where", "how", "date",
                            "time", "plan", "remember", "did", "do", "is"))


def strip_markdown(text: str) -> str:
    import re
    return re.sub(r"\*\*(.+?)\*\*|__(.+?)__|`(.+?)`", lambda m: next(
        g for g in m.groups() if g is not None), str(text or ""))


def relationship_line(sender_name: str) -> str:
    """Who the sender is to the owner, from the registry graph — best
    effort, one line ("Ruma Roy is your wife")."""
    try:
        from kyraan.store import persons, triples
        pid = persons.resolve(sender_name)
        if not pid:
            first = sender_name.split()[0] if sender_name else ""
            pid = persons.resolve(first) if first else None
        if not pid:
            return ""
        rows = triples.relations_for(pid) or []
        for r in rows:
            head, rel, tail = r.get("head"), r.get("relation", ""), r.get("tail")
            if head == pid and tail == "owner":
                return f"{sender_name} is your {rel.replace('_of', '').replace('_', ' ')}."
            if head == "owner" and tail == pid:
                return f"{sender_name}: you are their {rel.replace('_of', '').replace('_', ' ')}."
        return f"{sender_name} is in your people registry ({pid})."
    except Exception:
        return ""


def build_instruction(channel: str, mention: dict, thread: list,
                      owner_samples: list) -> str:
    """The writer brief: sender, relationship, the recent thread, and
    the owner's own recent messages as a voice sample."""
    rel = relationship_line(mention["user"])
    thread_txt = "\n".join(
        f"- {r['user']}{' (you, already said)' if r.get('ours') else ''}: "
        f"{r['text'][:200]}" for r in thread[-6:]) or "(no earlier messages)"
    voice = "\n".join(f"- {t[:160]}" for t in owner_samples[-5:]) \
        or "(no samples — plain, warm, brief)"
    return (
        f"Slack {channel}. {mention['user']} just wrote to you: "
        f"\"{mention['text'][:600]}\"\n"
        + (f"Relationship: {rel}\n" if rel else "")
        + f"Recent thread (oldest first):\n{thread_txt}\n"
        f"Your own recent messages here (match this voice):\n{voice}\n\n"
        "Write the reply YOU (the owner) would send, in first person, in "
        "the same language/register the sender used, as a real person "
        "texts — short, natural, no assistant phrasing, no sign-off. "
        "Answer from WHAT YOU KNOW (facts and documents below) when it "
        "covers the question — never promise to \"check\" something you "
        "already know. Don't repeat anything you already said earlier in "
        "the thread — lines marked (you, already said) are DONE; never "
        "bring them up again unless asked. Respond to what they SAID: an "
        "emotional message gets warmth and a question back, not a task "
        "update. Match length to theirs — a short message gets one or "
        "two short lines. Plain text, no markdown. If you genuinely lack "
        "a fact, say what a person would (\"let me check and tell you\") "
        "— never ask the reader of this brief anything. Output the "
        "message text only.")


async def tick(channels: list, owner_chat: int) -> int:
    """One poll over `channels` (#names). Returns mentions surfaced."""
    if not kernel.can_send_proactively(chat_id=owner_chat):
        return 0  # DND/kill switch: watermarks untouched, nothing missed
    state = _load()
    marks = state.setdefault("watermarks", {})
    surfaced = 0
    for channel in channels:
        try:
            raw = await kernel.run_tool(kernel.ToolCall(
                "slack.history", {"channel_id": channel, "limit": "1d"}),
                meta=True)
        except Exception as exc:
            log_event("slack_watch_read_failed", channel=channel,
                      error=str(exc)[:100])
            continue
        rows = parse_history(raw)
        newest = max((r["ts"] for r in rows), default="")
        if channel not in marks:
            # first sight: never replay history — start from now
            marks[channel] = newest
            continue
        fresh = [r for r in rows if r["ts"] > marks[channel]
                 and r["user_id"] != _owner_user_id]
        failed = False
        ordered = sorted(rows, key=lambda x: x["ts"])
        for r in sorted(fresh, key=lambda x: x["ts"]):
            if not mentions_owner(r["text"], _owner_user_id, _owner_handle):
                continue
            ours = set(state.get("kyraan_posted", []))
            thread = [{**x, "ours": x["text"] in ours}
                      for x in ordered if x["ts"] < r["ts"]]
            samples = [x["text"] for x in ordered
                       if x["user_id"] == _owner_user_id
                       and x["text"] not in ours]
            instruction = build_instruction(channel, r, thread, samples)
            question = r["text"].replace(f"<@{_owner_user_id}>", "").strip()
            try:
                draft = ""
                posted = state.get("kyraan_posted", [])
                for attempt in range(3):
                    draft = strip_markdown(
                        (await _draft_fn(instruction, question) or "").strip())
                    why = ("addressed the owner, not the sender"
                           if looks_like_meta_talk(draft) else
                           "repeated lines you already posted earlier"
                           if repeats_earlier(draft, posted) else
                           "far too long for what they wrote"
                           if out_of_proportion(draft, question) else
                           "" if draft else "empty")
                    if not why:
                        break
                    log_event("slack_watch_draft_rejected", attempt=attempt,
                              why=why, draft=draft[:120])
                    instruction += (f"\n\nREJECTED: that {why}. Write ONLY "
                                    "what the owner would text back — new "
                                    "words, short, to what they said.")
                else:
                    draft = ""  # two misses: surface without a draft
                await _ask_fn(owner_chat, channel, draft,
                              f"{r['user']}: {r['text'][:300]}")
            except Exception as exc:
                log_event("slack_watch_surface_failed", channel=channel,
                          error=str(exc)[:100])
                failed = True
                break  # keep order; retry this mention next tick
            surfaced += 1
            log_event("slack_watch_mention", channel=channel, by=r["user"])
        if newest and not failed:
            # delivery truth: the watermark advances only past messages
            # that were surfaced (or needed no surfacing)
            marks[channel] = max(marks[channel], newest)
    _save(state)
    return surfaced
