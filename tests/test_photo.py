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
    reply, enroll = await photo.answer(9, "data:image/jpeg;base64,AAA", "what is this?")
    assert reply == "That's a red square."   # non-JSON response: fallback keeps the text
    assert enroll is None
    assert seen["tier"] == "frontier"
    assert seen["images"] == ["data:image/jpeg;base64,AAA"]
    assert "what is this?" in seen["prompt"]
    assert "never instructions" in seen["system"]   # the taint rule rides along


async def test_vision_extracts_enrollment_intent_from_any_caption(monkeypatch):
    """The caption path's intelligence: the vision model returns JSON with
    the reply AND any enrollment intent — regex phrases no longer the
    only door (live 2026-08-26: 'save this face, it's Maan' had no path)."""
    class _R:
        text = '{"reply": "A man in a green blazer.", "remember_face_as": "Maan"}'
        latency_ms = 5.0

    async def fake_acall(**kw):
        assert kw.get("force_json") is True
        return _R()

    monkeypatch.setattr(photo.router, "acall", fake_acall)
    reply, enroll = await photo.answer(9, "data:x", "save this face, it's Maan")
    assert reply == "A man in a green blazer."
    assert enroll == "Maan"

    class _R2:
        text = '{"reply": "A toddler crawling.", "remember_face_as": null}'
        latency_ms = 5.0

    async def fake_acall2(**kw):
        return _R2()

    monkeypatch.setattr(photo.router, "acall", fake_acall2)
    reply, enroll = await photo.answer(9, "data:x", "this is kiaan")
    assert enroll is None   # naming alone is not an enrollment request


async def test_kill_switch_blocks_photo_turns(monkeypatch):
    monkeypatch.setattr(photo.kill_switch, "is_engaged", lambda: True)
    reply = await photo.answer(9, "data:x", "hi")
    assert "kill switch" in str(reply).lower()


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
    # high since 2026-08-28: low downscaled to ~512px and garbled
    # package/label lettering ("OMNIGEL" -> "OMMLNIL")
    assert content[1]["image_url"]["detail"] == "high"


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


async def test_empty_vision_answer_retries_once(monkeypatch):
    """A successful call with a BLANK reply gets one in-process retry
    (found live 2026-08-27: 7s call, empty answer, the owner had to
    resend the photo); a second blank still gets the honest apology."""
    calls = []

    async def blank_then_good(prompt="", **kw):
        calls.append(1)
        text = '{"reply": ""}' if len(calls) == 1 else '{"reply": "A receipt."}'
        return SimpleNamespace(text=text, latency_ms=1.0)

    monkeypatch.setattr(photo.router, "acall", blank_then_good)
    reply, _ = await photo.answer(9, "data:image/jpeg;base64,AAA", "")
    assert reply == "A receipt."
    assert len(calls) == 2

    calls.clear()

    async def always_blank(prompt="", **kw):
        calls.append(1)
        return SimpleNamespace(text='{"reply": ""}', latency_ms=1.0)

    monkeypatch.setattr(photo.router, "acall", always_blank)
    reply, _ = await photo.answer(9, "data:image/jpeg;base64,AAA", "")
    assert "couldn't read that photo" in reply
    assert len(calls) == 2  # exactly one retry, never a loop


async def test_command_caption_yields_title_not_the_command(monkeypatch):
    """Live 2026-08-28 01:49: caption "save this supliment for kian"
    became the document's NAME. A command caption is an instruction:
    the vision title names the doc; the caption still supplies
    subjects (via ingest's own resolution)."""
    ingested = {}

    def fake_ingest(chat_id, kind, text, caption="", subjects=None,
                    original=None, **kw):
        ingested.update(caption=caption, subjects=subjects)
        return "doc-1"

    from kyraan.store import documents
    monkeypatch.setattr(documents, "ingest", fake_ingest)
    monkeypatch.setattr(documents, "subjects_from_name",
                        lambda title: ["kiaan"] if "kian" in title.lower() else [])

    class _R:
        text = ('{"reply": "A supplement box.", "remember_face_as": null, '
                '"document_text": "Fourts B Drops Zinc Vitamin C", '
                '"document_title": "Fourts B Drops box", '
                '"document_subjects": []}')
        latency_ms = 5.0

    async def fake_acall(**kw):
        return _R()

    monkeypatch.setattr(photo.router, "acall", fake_acall)
    reply, _ = await photo.answer(9, "data:image/jpeg;base64,AAAA",
                                  "save this supliment for kian")
    assert ingested["caption"] == "Fourts B Drops box"
    assert "kiaan" in ingested["subjects"]
