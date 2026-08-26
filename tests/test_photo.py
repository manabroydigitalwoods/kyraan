"""Photo analysis — the vision call path, the no-tools-by-construction
taint property, kill-switch gating, and the router's image plumbing."""
from types import SimpleNamespace

import pytest

from kyraan.agents import orchestrator, photo
from kyraan.model_router import router


async def test_answer_sends_image_to_frontier(monkeypatch):
    seen = {}

    class _R:
        text = "That's a red square."
        latency_ms = 42.0

    async def fake_acall(prompt="", system="", tier="", images=None, **kw):
        seen.update(tier=tier, images=images, system=system, prompt=prompt)
        return _R()

    monkeypatch.setattr(photo.router, "acall", fake_acall)
    reply = await photo.answer(9, "data:image/jpeg;base64,AAA", "what is this?")
    assert reply == "That's a red square."
    assert seen["tier"] == "frontier"
    assert seen["images"] == ["data:image/jpeg;base64,AAA"]
    assert "what is this?" in seen["prompt"]
    assert "never instructions" in seen["system"]   # the taint rule rides along


async def test_kill_switch_blocks_photo_turns(monkeypatch):
    monkeypatch.setattr(photo.kill_switch, "is_engaged", lambda: True)
    reply = await photo.answer(9, "data:x", "hi")
    assert "kill switch" in reply.lower()


async def test_provider_error_becomes_vision_unavailable(monkeypatch):
    async def broken(**kw):
        raise router.ModelProviderError("no images")

    monkeypatch.setattr(photo.router, "acall", broken)
    with pytest.raises(photo.VisionUnavailable):
        await photo.answer(9, "data:x", "")


def test_router_rejects_images_on_non_openai_providers():
    with pytest.raises(router.ModelProviderError, match="cannot process images"):
        router._dispatch("ollama", "qwen3:8b", "p", "s", 100, images=["data:x"])


def test_openai_payload_carries_image_parts(monkeypatch):
    captured = {}

    class _Msg:
        content = "red"
        reasoning = None

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]
        usage = None

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setattr(router, "_get_openai_compatible_client",
                        lambda provider, cfg: _Client())
    router._call_openai_compatible(
        "openai", {"max_tokens_param": "max_completion_tokens"},
        "gpt-5.4-nano", "what is it?", "sys", 100,
        images=["data:image/png;base64,AA"])
    content = captured["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "what is it?"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,AA"
    assert content[1]["image_url"]["detail"] == "low"


def test_record_exchange_lands_in_history_and_chat_log(monkeypatch):
    orchestrator._history.pop(94, None)
    orchestrator.record_exchange(94, "[sent a photo: what's this?]", "A red square.")
    assert list(orchestrator._history[94]) == [
        ("user", "[sent a photo: what's this?]"),
        ("assistant", "A red square.")]
    from kyraan.control_plane import logging_setup
    import json
    rows = [json.loads(l) for l in logging_setup.CHAT_LOG.read_text().splitlines()]
    assert rows[-1]["role"] == "assistant" and rows[-1]["chat_id"] == 94
    orchestrator._history.pop(94, None)


def test_brief_flips_the_image_denial(monkeypatch):
    from kyraan.agents import capabilities

    base = capabilities.config.load()
    monkeypatch.setattr(capabilities.config, "load", lambda: {
        **base, "model_tiers": {"cheap": {"provider": "ollama"},
                                "frontier": {"provider": "openai"}}})
    brief = capabilities.capability_brief()
    assert "analyze PHOTOS" in brief
    assert "you can SEE photos" in brief
    assert "cannot create, draft, see" not in brief

    monkeypatch.setattr(capabilities.config, "load", lambda: {
        **base, "model_tiers": {"cheap": {"provider": "ollama"},
                                "frontier": {"provider": "ollama"}}})
    brief = capabilities.capability_brief()
    assert "analyze PHOTOS" not in brief
    assert "cannot create, draft, see" in brief
