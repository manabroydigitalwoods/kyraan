"""Per-chat session state — the conversation window, rolling summaries,
and the transcript seeding/recording that feeds them.

Split out of orchestrator.py (2026-08-27) ahead of Phase 3: plan.md §3
names a Session/State Store (Redis) for exactly this state — short-term
conversation memory that is allowed to vanish. When Phase 3 stands Redis
up, this module is the swap point; until then it stays process-memory +
one JSON file, as before. Orchestrator re-exports every name here, so
callers and tests keep addressing session state through orchestrator.
"""
import json
from collections import defaultdict, deque

from kyraan.control_plane import logging_setup
from kyraan.control_plane.logging_setup import log_chat, log_event
from kyraan.model_router import router

# Rolling per-chat conversation window: the qa.answer prompt's only session
# memory. In-memory on purpose (like _pending_confirmations) — a restart
# forgets the conversation, which is honest, and durable facts are the
# memory tree's job, not this window's.
_HISTORY_MAX_ENTRIES = 40  # 20 exchanges — 20 rolled out mid-session live
                           # ("you never shared Fpol data" after 17 turns)
_history: dict = defaultdict(lambda: deque(maxlen=_HISTORY_MAX_ENTRIES))

# C (harness pack): entries pushed off the history window collect here
# until a chunk is worth condensing into the rolling session summary.
_summary_backlog: dict = defaultdict(list)
_SUMMARY_CHUNK = 10
_SUMMARIES_PATH = None  # resolved lazily (test isolation patches the dir)


def _summaries_path():
    from pathlib import Path
    return Path(__file__).resolve().parents[3] / "data" / "session_summaries.json"


def _load_summaries() -> dict:
    try:
        return json.loads(_summaries_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def session_summary(chat_id: int) -> str:
    return _load_summaries().get(str(chat_id), "")


async def _roll_summary(chat_id: int) -> None:
    """Condense the backlog chunk into the running summary — LOCAL tier
    only (free, and the summary may contain anything the conversation
    did). Best-effort: a failure drops nothing user-visible."""
    backlog = _summary_backlog.get(chat_id) or []
    if len(backlog) < _SUMMARY_CHUNK:
        return
    chunk, _summary_backlog[chat_id] = backlog[:_SUMMARY_CHUNK], backlog[_SUMMARY_CHUNK:]
    rendered = "\n".join(f"{role}: {text[:300]}" for role, text in chunk)
    previous = session_summary(chat_id)
    try:
        response = await router.acall(
            prompt=(f"Existing summary:\n{previous or '(none)'}\n\n"
                    f"Older messages leaving the window:\n{rendered}"),
            system=("Maintain ONE running summary of this conversation's older "
                    "context for a personal assistant: decisions, ongoing topics, "
                    "user statements that may matter later. Merge the new "
                    "messages into the existing summary. Max 120 words, plain "
                    "text, no preamble."),
            tier="cheap", max_tokens=400)
        summary = response.text.strip()[:1200]
        if summary:
            from kyraan.control_plane.filelock import atomic_write_text, locked
            with locked(_summaries_path()):
                summaries = _load_summaries()
                summaries[str(chat_id)] = summary
                _summaries_path().parent.mkdir(exist_ok=True)
                atomic_write_text(_summaries_path(), json.dumps(summaries, ensure_ascii=False, indent=1))
            log_event("session_summary_rolled", chat_id=chat_id, chars=len(summary))
    except Exception as exc:
        _summary_backlog[chat_id] = chunk + _summary_backlog[chat_id]  # retry later
        log_event("session_summary_error", error=str(exc)[:150])


def _legacy_cloud_placeholder(text: str) -> str | None:
    """Pre-cloud_text log entries whose bodies must not re-enter
    model-visible history at seed time (security rounds: email listings,
    then round-5's catch — pending-review listings and decision receipts
    written before their redaction existed)."""
    import re
    if re.search(r"You have about \d+ unread", text) or "Latest unread:" in text:
        return "[showed the unread email summary]"
    if text.startswith("Facts awaiting your review:") or "Pending facts awaiting" in text:
        return "[showed the pending-review list]"
    if text.startswith("✅ Saved to memory:") or text.startswith("🗑 Rejected:") \
            or "Saved to memory:" in text.split("\n")[0]:
        return "[applied the owner's review decisions]"
    return None


def seed_history_from_log(max_per_chat: int = 40) -> None:
    """Rebuild in-memory conversation history from chat.jsonl at startup.

    Found live 2026-08-26: five minutes after a service restart, 'are
    those the latest emails?' got a fabricated 'No, those are not the
    latest' — the restart had wiped _history, so qa was judging a listing
    it could not see. The log on disk has the whole conversation; a
    restart should be invisible to the user."""
    path = logging_setup.CHAT_LOG
    if not path.exists():
        return
    per_chat: dict = defaultdict(list)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        log_event("history_seed_failed", error=str(exc))
        return
    for line in lines[-2000:]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = entry.get("role")
        if role == "proactive":
            role = "assistant"
        if role in ("user", "assistant") and entry.get("text"):
            text = entry.get("cloud_text") or entry["text"]
            if role == "assistant" and "cloud_text" not in entry:
                placeholder = _legacy_cloud_placeholder(text)
                if placeholder:
                    # Pre-upgrade entries carry full bodies with no cloud
                    # twin — recognized by their fixed templates.
                    text = placeholder
            per_chat[entry["chat_id"]].append((role, text))
    for chat_id, entries in per_chat.items():
        if not _history[chat_id]:  # never clobber a live conversation
            _history[chat_id].extend(entries[-max_per_chat:])
    log_event("history_seeded", chats=len(per_chat))


def record_exchange(chat_id: int, user_text: str, assistant_text: str) -> None:
    """Record a user/assistant exchange that happened OUTSIDE
    handle_message (photo turns) — same history/backlog/chat-log
    treatment, so follow-ups and the rolling summary see it."""
    for entry in (("user", user_text), ("assistant", assistant_text)):
        if len(_history[chat_id]) == _HISTORY_MAX_ENTRIES:
            _summary_backlog[chat_id].append(_history[chat_id][0])
        _history[chat_id].append(entry)
    log_chat(chat_id, "user", user_text)
    log_chat(chat_id, "assistant", assistant_text)


def record_proactive(chat_id: int, text: str) -> None:
    """Proactive sends (reminders, briefs) belong in conversation history
    too — found live: \"Thanks for the reminder\" got \"I didn't actually
    send you any reminders\" because fire() bypassed _history entirely."""
    _history[chat_id].append(("assistant", text))
    log_chat(chat_id, "proactive", text)


def _history_block(chat_id: int, clip: int = 600, older_clip: int | None = None) -> str:
    """Per-entry clip: one pasted article must not drown the prompt (and
    with Ollama's default 4K context it literally truncated the system
    instructions — the likely cause of a live garbled reply).

    older_clip: tighter cap for everything but the last 8 entries — recent
    turns carry the follow-up context and stay at full clip; old turns keep
    their gist. Token thrift without dropping what's actually used."""
    entries = list(_history[chat_id])
    lines = []
    summary = session_summary(chat_id)
    if summary:
        lines.append(f"[Earlier in this conversation, summarized: {summary}]")
    for i, (role, text) in enumerate(entries):
        cap = clip
        if older_clip is not None and i < len(entries) - 8:
            cap = older_clip
        lines.append(f"{role}: {text[:cap] + '…' if len(text) > cap else text}")
    return "\n".join(lines) or "(no conversation yet)"


def _classifier_context(chat_id: int, entries: int = 6, clip: int = 200) -> str:
    """Compact tail of the conversation for intent classification — enough
    to resolve a follow-up, clipped so a long calendar listing or code
    answer doesn't drown the classifier prompt."""
    recent = list(_history[chat_id])[-entries:]
    return "\n".join(
        f"{role}: {text[:clip] + '…' if len(text) > clip else text}" for role, text in recent
    )
