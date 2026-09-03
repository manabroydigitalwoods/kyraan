"""Secrets (owner 2026-09-03: "ek secret baat hai ... isko secret rakho";
then "what do we do for secret data ... end to end secret?").

What happened live: the assistant PROMISED secrecy while every message
of the exchange went to the cloud tier as a prompt, sat in the
conversation window for the next twenty exchanges (re-entering cloud
prompts each turn), was chunked into a cloud_ok episode for recall,
and produced two facts for review. Nothing understood "keep this
secret" as an instruction to the machinery. This module does.

A SECRET WINDOW is per chat, owner-only, opened by a secret phrase in
any wording (English / Hinglish), extended by every message inside it,
closed by silence (15 min) or an explicit "bas itna hi / that's all".
Inside the window every turn is handled END TO END on this machine:

  * the reply comes from the LOCAL tier only — no cloud call at all;
  * the conversation window and the chat log carry a PLACEHOLDER for
    both sides (`cloud_text`), so no later cloud prompt, seeding after a
    restart, or episode chunk ever sees the words;
  * fact extraction is skipped — nothing about it enters memory unless
    the owner later says so explicitly;
  * the local model is told to acknowledge briefly, ask nothing, and
    never bring it up.

A RETROACTIVE request ("isko secret rakho", "keep this between us"
about what was just said) also redacts the exchanges before it: the
window entries become placeholders and a `redact` record in the chat
log makes seeding and episode building apply the same placeholder.
"""
import re
import time

from kyraan.control_plane.logging_setup import log_chat, log_event
from kyraan.store import redis_kv

PLACEHOLDER = "[a private matter the owner asked to keep secret]"
WINDOW_S = 15 * 60
RETRO_ENTRIES = 4   # the two exchanges before "keep this secret"

_OPENS = re.compile(
    # An INSTRUCTION shape, never a bare noun (review 2026-09-03: "what's
    # the secret to good biryani", "list my github secrets", "the meeting
    # between us and the vendor" would each have flipped the turn to a
    # tool-less local window).
    r"\b(?:ek|a|one|my)\s+secret\b(?!\s+(?:ingredient|recipe|sauce|santa|to\b))"
    r"|\bsecret\s+baat\b|\bprivate\s+baat\b|\bgupt\s+baat\b|\braaz\s+ki\s+baat\b"
    r"|\bi\s+have\s+a\s+secret\b|\bthis\s+is\s+(?:a\s+)?(?:secret|confidential)\b"
    r"|\b(?:keep|rakh\w*)\b.{0,30}\b(?:secret|confidential|private|between\s+us)\b"
    r"|\b(?:secret|confidential)\b.{0,20}\b(?:rakh\w*|rakha)\b"
    r"|\b(?:stays?|is|strictly|just|only)\s+between\s+us\b|\bbetween\s+us\s+only\b"
    r"|\boff\s+the\s+record\b|\bdon'?t\s+tell\s+(?:anyone|anybody|nobody)\b|\btell\s+no\s*one\b",
    re.IGNORECASE)
_RETRO = re.compile(
    # about what was JUST said: an object pronoun bound to keep/rakho
    r"\b(?:keep|rakh\w*)\s+(?:this|that|it|these|isko|isse|ye|yeh|is\s+baat)\b"
    r"|\b(?:isko|isse|is\s+baat|ye|yeh|this|that|it)\s+(?:ko\s+)?(?:secret|confidential)\s+(?:rakh\w*|rakha)\b"
    r"|\b(?:this|that|it)\s+(?:stays|is)\s+(?:strictly\s+)?(?:between\s+us|off\s+the\s+record)\b",
    re.IGNORECASE)
_CLOSES = re.compile(
    r"^\s*(?:bas\s+itna\s*hi|bas|that'?s\s+all|that\s+is\s+all|done|ok\s+done|"
    r"secret\s+over|end\s+of\s+secret|thik\s+hai\s+bas)\s*[.!]*\s*$", re.IGNORECASE)

_mem: dict = {}
_private_mem: dict = {}
_PRIVATE_CMD = re.compile(
    r"^\s*(?:private|secret)\s+mode\s+(on|off)\s*[.!]*\s*$", re.IGNORECASE)


def private_command(text: str) -> str | None:
    """"private mode on" / "private mode off" -> "on" | "off" | None."""
    m = _PRIVATE_CMD.match(str(text or ""))
    return m.group(1).lower() if m else None


def _pkey(chat_id: int) -> str:
    return redis_kv.key("private", chat_id)


def private_active(chat_id: int) -> bool:
    """The explicit switch (owner 2026-09-03: "fast and easy"): every turn
    stays on this Mac until "private mode off". No timeout."""
    v = redis_kv.get_json(_pkey(chat_id))
    if v is None:
        v = _private_mem.get(chat_id)
    return bool(v)


def set_private(chat_id: int, on: bool) -> None:
    if on:
        _private_mem[chat_id] = True
        try:
            redis_kv.set_json(_pkey(chat_id), True)
        except Exception:
            pass
    else:
        _private_mem.pop(chat_id, None)
        try:
            redis_kv.delete(_pkey(chat_id))
        except Exception:
            pass


def opens(text: str) -> bool:
    t = str(text or "").strip()
    if t.endswith("?") and not re.search(r"\b(?:rakh\w*|keep)\b", t, re.IGNORECASE):
        return False               # a question about secrets is not a secret
    return bool(_OPENS.search(t))


def retro(text: str) -> bool:
    """A request about what was JUST said — "isko secret rakho", "keep
    this between us" — as opposed to announcing a secret to come."""
    return opens(text) and bool(_RETRO.search(str(text or "")))


def closes(text: str) -> bool:
    return bool(_CLOSES.match(str(text or "")))


def _key(chat_id: int) -> str:
    return redis_kv.key("secret", chat_id)


def active(chat_id: int) -> bool:
    """A secret window is open, or private mode is on."""
    if private_active(chat_id):
        return True
    until = redis_kv.get_json(_key(chat_id))
    if until is None:
        until = _mem.get(chat_id)
    return bool(until) and float(until) > time.time()


def touch(chat_id: int) -> None:
    until = time.time() + WINDOW_S
    _mem[chat_id] = until
    try:
        redis_kv.set_json(_key(chat_id), until, ttl_s=WINDOW_S)
    except Exception:
        pass


def close(chat_id: int) -> None:
    _mem.pop(chat_id, None)
    try:
        redis_kv.delete(_key(chat_id))
    except Exception:
        pass


def redact_recent(chat_id: int, entries: int = RETRO_ENTRIES,
                  contains: str = "") -> int:
    """Replace the cloud-visible text of recent window entries with the
    placeholder and write a `redact` record so the chat log's readers
    (seeding, episodes) do the same. `contains` redacts every earlier
    entry holding that substring instead of the last N. The owner's
    local screen and the local log line keep the words."""
    from kyraan.agents import session
    hist = list(session._history[chat_id])
    if contains:
        needle = contains.lower()
        targets = [i for i, (_, t) in enumerate(hist) if needle in str(t).lower()]
    else:
        targets = list(range(max(0, len(hist) - entries), len(hist)))
    if not targets:
        return 0
    new = [(role, PLACEHOLDER) if i in targets else (role, text)
           for i, (role, text) in enumerate(hist)]
    session._history[chat_id] = new
    log_chat(chat_id, "redact", "",
             **({"contains": contains} if contains else {"count": len(targets)}))
    log_event("secret_redacted", chat_id=chat_id, entries=len(targets),
              mode="contains" if contains else "recent")
    return len(targets)


def apply_redactions(records: list) -> list:
    """Chat-log records with `redact` records applied: the targeted
    earlier user/assistant records of that chat gain cloud_text =
    PLACEHOLDER. Pure; the log itself is never rewritten."""
    out = []
    for entry in records:
        if entry.get("role") != "redact":
            out.append(dict(entry))
            continue
        chat_id = entry.get("chat_id")
        needle = str(entry.get("contains") or "").lower()
        count = int(entry.get("count") or 0)
        idx = [i for i, e in enumerate(out)
               if e.get("chat_id") == chat_id
               and e.get("role") in ("user", "assistant", "proactive")]
        if needle:
            hit = [i for i in idx if needle in str(out[i].get("text", "")).lower()
                   or needle in str(out[i].get("cloud_text", "")).lower()]
        else:
            hit = idx[-count:] if count else []
        for i in hit:
            out[i]["cloud_text"] = PLACEHOLDER
    return out


SYSTEM_ADDENDUM = (
    "\n\nSECRET MODE: the owner asked to keep this private. This message is "
    "being handled entirely on this machine and will not be remembered. "
    "Reply in ONE or two short sentences: acknowledge, keep confidence, "
    "answer only what was asked. Ask NO follow-up questions, give no "
    "advice or opinions unless asked, and never refer to this later.")


_LOCAL_SYSTEM = (
    "You are Kyraan, the owner's personal assistant, in PRIVATE MODE: this "
    "conversation is handled entirely on the owner's own machine and is not "
    "remembered. Reply in the owner's language (English or Hinglish as they "
    "write), in one to three short sentences. Keep confidence. Answer what "
    "was asked; if it is a statement, acknowledge it warmly and briefly. "
    "Ask no follow-up questions, give no unsolicited advice, and never "
    "moralise. You have no tools here; if something needs a tool, say so "
    "in one line.")


async def local_reply(chat_id: int, raw_text: str) -> str | None:
    """The private turn's model call (live 2026-09-03 05:23: the full
    agent-loop prompt — 11.5k tokens of tools, facts and history — took
    the local model 73 s and yielded nothing). A secret turn needs none
    of that: a short system line, the last few window entries (already
    placeholders where they were private), the message. Plain text, two
    attempts, None when the local model gives nothing."""
    from kyraan.agents import session
    from kyraan.model_router import router
    recent = list(session._history[chat_id])[-6:]
    convo = "\n".join(f"{r}: {str(t)[:300]}" for r, t in recent)
    prompt = ((f"Recent conversation:\n{convo}\n\n" if convo else "")
              + f"OWNER: {raw_text}")
    for attempt in range(2):
        try:
            resp = await router.acall(prompt=prompt, system=_LOCAL_SYSTEM,
                                      tier="cheap", max_tokens=300)
        except Exception as exc:
            log_event("secret_local_failed", attempt=attempt, error=str(exc)[:120])
            continue
        text = (resp.text or "").strip()
        if text:
            return text
        log_event("secret_local_empty", attempt=attempt)
    return None
