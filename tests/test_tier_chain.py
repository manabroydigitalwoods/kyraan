"""frontier -> standby -> cheap (2026-09-04, the mini trial)."""
import asyncio


def test_tier_chain_follows_config(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.control_plane import config
    monkeypatch.setattr(config, "load", lambda: {"model_tiers": {"frontier": {}, "standby": {}, "cheap": {}}})
    assert orchestrator.tier_chain() == ("frontier", "standby", "cheap")
    monkeypatch.setattr(config, "load", lambda: {"model_tiers": {"frontier": {}, "cheap": {}}})
    assert orchestrator.tier_chain() == ("frontier", "cheap")


def test_standby_answers_when_frontier_is_down(monkeypatch):
    from kyraan.agents import orchestrator, session, agent_loop
    from kyraan.control_plane import kernel
    monkeypatch.setenv("KYRAAN_SESSION_BACKEND", "memory")
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    monkeypatch.setattr(orchestrator, "log_chat", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "tier_chain", lambda: ("frontier", "standby", "cheap"))
    tried = []

    async def fake_run(chat_id, raw_text, tier="frontier", read_only=False):
        tried.append(tier)
        if tier == "frontier":
            raise agent_loop.AgentUnavailable("capped")
        return f"answered by {tier}"
    monkeypatch.setattr(agent_loop, "run", fake_run)
    session._history[81] = []
    out = asyncio.run(orchestrator.handle_message(81, "tell me something about siliguri weather patterns"))
    assert out == "answered by standby" and tried == ["frontier", "standby"]
    assert orchestrator.processing_marker(81).startswith("☁️")          # standby is still the cloud
    session._history[81] = []


def test_live_config_has_the_standby_tier():
    from kyraan.control_plane import config
    tiers = config.load()["model_tiers"]
    assert tiers["frontier"]["model"] == "gpt-5.4-mini"
    assert tiers["standby"]["model"] == "gpt-5.4-nano" and tiers["standby"]["provider"] == "openai"
