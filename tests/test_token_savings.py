"""Token audit 2026-09-04: direct renderings and the cache keep-warm."""
import asyncio
import time

from kyraan.agents import loop_tools as lt


def test_direct_render_only_for_plain_listing_asks():
    assert lt.direct_render("reminders.list", [], "my reminders") == "No reminders set."
    assert lt.direct_render("reminders.list", [], "show me my reminders for today") == "No reminders set."
    assert lt.direct_render("reminders.list", [], "am I free tomorrow?") is None
    assert lt.direct_render("tasks.list", [{"instruction": "x", "when": "Sat 8 PM", "repeats": "daily"}], "list tasks") \
        == "Scheduled tasks:\n• x — Sat 8 PM (daily)"
    assert lt.direct_render("home.get_state", {"name": "AC", "state": "on", "last_changed": "2:53 PM"}, "is the ac on?") \
        == "AC is ON (since 2:53 PM)."
    assert lt.direct_render("home.get_state", {"name": "AC", "state": "on"}, "why is the ac on?") is None
    ev = [{"title": "Kiaan birthday planning", "start": "2026-09-05T17:00:00+05:30", "all_day": False}]
    assert lt.direct_render("calendar.list_events", ev, "what's my calendar tomorrow") == "Calendar:\n• Sat 5 Sep, 5 PM — Kiaan birthday planning"
    assert lt.direct_render("web.search", {"results": []}, "list results") is None


def test_keep_warm_uses_the_live_prefix_and_only_while_active(monkeypatch):
    from kyraan.agents import agent_loop, orchestrator
    from kyraan.model_router import router
    from kyraan.triggers import cache_warm
    calls = []

    class R: cached_tokens = 7000; input_tokens = 7100

    async def fake_acall(**kw): calls.append(kw); return R()
    monkeypatch.setattr(router, "acall", fake_acall)
    monkeypatch.setattr(cache_warm, "_frontier_is_cloud", lambda: True)
    cache_warm._last_warm["at"] = 0.0
    monkeypatch.setattr(orchestrator, "last_owner_turn_at", time.time() - 3 * 3600)
    assert asyncio.run(cache_warm.tick()) is False and not calls            # idle: silent
    monkeypatch.setattr(orchestrator, "last_owner_turn_at", time.time() - 60)
    assert asyncio.run(cache_warm.tick()) is True
    assert calls[0]["system"] == agent_loop.warm_system() and calls[0]["max_tokens"] == 1
    assert asyncio.run(cache_warm.tick()) is False                          # not twice inside the interval
    live = agent_loop.build_system(read_only=False, stage="owner", tier="frontier", secret=False)
    assert live == agent_loop.warm_system()                                 # byte-identical prefix
