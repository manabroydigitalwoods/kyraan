"""Voice in the room — duty #4 (owner 2026-09-03, "go voice in the
room"), without an Alexa skill.

The Alexa Media Player integration exposes, on each Echo, the text of
the last thing said to it (`last_called_summary`) and when
(`last_called_timestamp`). Kyraan polls that every few seconds. An
utterance that starts with its name — "Alexa, Kyraan what's open" —
is handed to the owner's normal pipeline exactly as a Telegram
message would be (same rails, same tools, same confirm gate: "Alexa,
Kyraan yes" answers an ask), and the reply is SPOKEN on that Echo
through home.announce, and mirrored to Telegram so there is a record.

Boundaries, stated plainly: the words go through Amazon before they
reach this Mac — that is what an Echo is; nothing about this changes
what Kyraan sends to the cloud model afterwards. Alexa itself will
mumble "I don't know that one" before Kyraan answers; a routine on the
phrase "Kyraan" that just says "okay" quiets that. Speech is capped at
one announcement (240 chars); the rest is on Telegram.
"""
import re
import time

from kyraan.control_plane import kernel
from kyraan.control_plane.logging_setup import log_event

# Alexa's transcript of "Kyraan" (live 2026-09-04 00:13: "Kyraan house
# status" arrived as "current house status"). Every mishearing seen or
# likely is a wake word; "ask/tell Kyraan …" is how people address a
# skill and is accepted too.
WAKE = re.compile(
    r"^\s*(?:hey\s+|ok\s+|okay\s+|ask\s+|tell\s+)?"
    r"(?:kyraan|kyran|kyra|kiran|kieran|keeran|kira+n|kirin|karan|karen|current|curran|cairn|korean|kiara)"
    r"\b[\s,:.!-]*(?:to\s+)?(.*)$", re.IGNORECASE)
SPEECH_MAX = 240
_last_ts: dict = {}          # device -> last handled timestamp (process memory)
_boot = time.time()


def _cfg() -> dict:
    from kyraan.control_plane import config
    return (config.load().get("duties") or {}).get("voice_echo") or {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def devices() -> list:
    """media_player entities to listen on — every allowlisted Echo."""
    from kyraan.control_plane import config
    server = (config.load().get("tool_servers") or {}).get("home_assistant") or {}
    cfg = _cfg().get("devices")
    if cfg:
        return list(cfg)
    return [e for e in (server.get("read_entities") or [])
            if e.startswith("media_player.") and "echo" in e]


def poll_seconds() -> int:
    return max(2, int(_cfg().get("poll_seconds", 3)))


def parse_wake(summary: str) -> str | None:
    """The request after Kyraan's name, or None when it wasn't for us."""
    m = WAKE.match(str(summary or ""))
    if not m:
        return None
    text = m.group(1).strip()
    return text or None


def for_speech(reply: str) -> str:
    """Plain spoken text: no markdown, bullets become sentences, the
    processing marker and links dropped, one announcement long."""
    t = re.sub(r"\n\n(?:🔒|☁️)[^\n]*$", "", reply.strip())
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_`>]", "", t)
    t = re.sub(r"^\s*#{1,6}\s+", "", t, flags=re.M)      # headings, not "#dev"
    t = re.sub(r"^\s*[•\-–]\s*", "", t, flags=re.M)
    t = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\.\s*\.", ".", t).strip()
    if len(t) > SPEECH_MAX:
        cut = t.rfind(". ", 0, SPEECH_MAX - 30)
        t = (t[:cut + 1] if cut > 60 else t[:SPEECH_MAX - 30].rstrip()) + " The rest is on Telegram."
    return t


def _echo_name(entity: str) -> str:
    return entity.split(".", 1)[1]


async def read_last_called(entity: str) -> tuple:
    """(timestamp_ms, summary) from HA's full record of the Echo."""
    import asyncio
    from kyraan.tools import home_assistant as ha
    raw = await asyncio.to_thread(ha._raw, entity)
    attrs = raw.get("attributes") or {}
    try:
        ts = int(attrs.get("last_called_timestamp") or 0)
    except (TypeError, ValueError):
        ts = 0
    return ts, str(attrs.get("last_called_summary") or "")


async def tick(owner_chat: int, send_fn) -> int:
    """One poll over the Echos. Returns utterances handled."""
    if not enabled():
        return 0
    handled = 0
    for entity in devices():
        try:
            ts, summary = await read_last_called(entity)
        except Exception as exc:
            log_event("voice_echo_read_failed", device=entity, error=str(exc)[:100])
            continue
        if not ts:
            continue
        seen = _last_ts.get(entity)
        if seen is None:
            _last_ts[entity] = ts          # first sight after boot: never replay
            continue
        if ts <= seen:
            continue
        _last_ts[entity] = ts
        if ts / 1000 < _boot - 120:
            continue
        text = parse_wake(summary)
        if not text:
            # the owner's own words to Alexa, kept short, so mishearings
            # of the name can be added to WAKE
            log_event("voice_echo_ignored", device=entity, heard=summary[:40])
            continue
        log_event("voice_echo_heard", device=entity, chars=len(text))
        handled += 1
        await handle(owner_chat, entity, text, send_fn)
    return handled


async def handle(owner_chat: int, entity: str, text: str, send_fn) -> None:
    """Owner's pipeline, then speak the reply on the Echo it came from."""
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel as _kernel
    token = _kernel.set_viewer("owner", "owner")
    try:
        reply = await orchestrator.handle_message(owner_chat, text)
    except Exception as exc:
        reply = f"Something went wrong: {str(exc)[:80]}"
    finally:
        try:
            _kernel.reset_viewer_stage(token)
        except Exception:
            pass
    spoken = for_speech(reply)
    said = False
    try:
        await kernel.run_tool(kernel.ToolCall(
            "home.announce", {"message": spoken, "target": _echo_name(entity)}))
        said = True
    except Exception as exc:
        log_event("voice_echo_speak_failed", device=entity, error=str(exc)[:120])
    try:
        await send_fn(owner_chat, f"🎙 You said: {text}\n\n{reply}"
                      + ("" if said else "\n\n(couldn't speak this on the Echo — see above)"))
    except Exception as exc:
        log_event("voice_echo_mirror_failed", error=str(exc)[:100])
