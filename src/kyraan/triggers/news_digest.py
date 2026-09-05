"""Duty (owner 2026-09-05): the headlines digest, morning and evening —
config news.digest_times, default 07:45 and 19:45. Same proactive gate
and delivery truth as the other duties; the render is tools.news."""
from kyraan.control_plane import kernel
from kyraan.control_plane.logging_setup import log_event


def times() -> list:
    """[datetime.time…] from config news.digest_times; [] when off."""
    from datetime import time as _time
    from kyraan.tools import news
    s = news._settings()
    if not s["enabled"]:
        return []
    out = []
    for hhmm in s["digest_times"]:
        try:
            hh, mm = str(hhmm).split(":")
            out.append(_time(int(hh), int(mm)))
        except Exception:
            continue
    return out


async def fire(chat_id: int, send_fn) -> bool:
    if not kernel.can_send_proactively(chat_id=chat_id):
        return False
    import asyncio as _aio

    from kyraan.tools import news
    try:
        text = await _aio.to_thread(news.digest_text)
    except Exception as exc:
        log_event("news_digest_failed", error=str(exc)[:100])
        return False
    ok = await send_fn(chat_id, text)
    log_event("news_digest_sent", ok=bool(ok), chars=len(text))
    return bool(ok)
