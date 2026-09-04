"""Prompt-cache keep-warm (token audit 2026-09-04). OpenAI caches a
prompt prefix for 5–10 idle minutes; the first call of most turns
missed it and paid full price for ~8k tokens of system prompt. While
the owner is actively chatting (a turn in the last WINDOW_S), a
1-token call every INTERVAL_S re-touches the exact prefix the live
turns use, at the cached rate. Silent when idle, when the frontier is
local, or when the kill switch is engaged."""
import os
import time

from kyraan.control_plane.logging_setup import log_event

INTERVAL_S = 240
WINDOW_S = 30 * 60
_last_warm = {"at": 0.0}


def _frontier_is_cloud() -> bool:
    from kyraan.control_plane import config
    from kyraan.model_router import router
    provider = (config.load().get("model_tiers", {}).get("frontier") or {}).get("provider", "")
    return bool(provider) and not router.provider_is_local(provider)


async def tick(now: float | None = None) -> bool:
    now = now or time.time()
    from kyraan.agents import orchestrator
    if now - orchestrator.last_owner_turn_at > WINDOW_S:
        return False
    if now - _last_warm["at"] < INTERVAL_S - 5:
        return False
    try:
        from kyraan.control_plane import kernel
        if kernel.kill_switch.is_engaged() or not _frontier_is_cloud():
            return False
        from kyraan.agents import agent_loop
        from kyraan.model_router import router
        system = agent_loop.warm_system()
        prev = os.environ.get("KYRAAN_SPEND_BUCKET", "")
        os.environ["KYRAAN_SPEND_BUCKET"] = "cache_warm"
        try:
            resp = await router.acall(prompt="CONTEXT:\n(warm)", system=system, tier="frontier", max_tokens=1)
        finally:
            if prev:
                os.environ["KYRAAN_SPEND_BUCKET"] = prev
            else:
                os.environ.pop("KYRAAN_SPEND_BUCKET", None)
        _last_warm["at"] = now
        log_event("cache_warmed", cached_tokens=getattr(resp, "cached_tokens", None),
                  input_tokens=getattr(resp, "input_tokens", None))
        return True
    except Exception as exc:
        _last_warm["at"] = now          # never retry in a tight loop
        log_event("cache_warm_failed", error=str(exc)[:120])
        return False
