"""Private (local-only) documents: found by name, answered locally (2026-09-04)."""
import asyncio

from kyraan.store import documents


def test_name_words_drop_the_verbs():
    assert documents._name_words("explain the computation") == ["computation"]
    assert documents._name_words("what does the ITR-V say") == []      # 3-char stems: nothing to match on


def test_private_document_ask_goes_to_the_local_model(monkeypatch):
    from kyraan.agents import orchestrator, secrets
    from kyraan.control_plane import kernel
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    doc = {"caption": "Computation.pdf", "date": "2026-09-04", "text": "Total tax payable 12,345"}
    monkeypatch.setattr(documents, "local_only_match", lambda chat_id, q: doc if "computation" in q else None)
    prompts = []

    class R: text = "The total tax payable is 12,345."

    async def fake_acall(prompt="", system="", tier="", max_tokens=0, **kw):
        prompts.append((tier, prompt)); return R()
    from kyraan.model_router import router
    monkeypatch.setattr(router, "acall", fake_acall)
    out = asyncio.run(orchestrator.handle_message(1, "explain the computation"))
    assert out.startswith("🔒 The total tax payable")
    assert prompts and prompts[0][0] == "cheap" and "12,345" in prompts[0][1]


def test_follow_up_after_a_private_answer_stays_local(monkeypatch):
    from kyraan.agents import orchestrator, secrets
    from kyraan.control_plane import kernel
    from kyraan.model_router import router
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    doc = {"caption": "Computation.pdf", "date": "2026-09-04", "text": "Income from other sources 26,347"}
    monkeypatch.setattr(documents, "local_only_match", lambda chat_id, q: doc if "computation" in q else None)
    prompts = []

    class R:
        def __init__(self, t): self.text = t
    answers = iter(["Total income 11,51,350.", "Other sources: 26,347.", "NOT_ABOUT_DOCUMENT"])

    async def fake_acall(prompt="", system="", tier="", max_tokens=0, **kw):
        prompts.append(prompt); return R(next(answers))
    monkeypatch.setattr(router, "acall", fake_acall)
    secrets._last_private_doc.clear()
    assert asyncio.run(orchestrator.handle_message(1, "explain the computation")).startswith("🔒 Total")
    out = asyncio.run(orchestrator.handle_message(1, "what are the other sources of income?"))
    assert out == "🔒 Other sources: 26,347."
    assert "Earlier in this conversation" in prompts[1] and "Total income" in prompts[1]   # the thread rides along
    assert len(secrets._last_private_doc[1]["qa"]) == 2


def test_tool_answers_locally_when_only_a_private_document_matches(monkeypatch):
    from kyraan.agents import loop_tools, secrets
    from kyraan.model_router import router
    doc = {"caption": "ITR-V.pdf", "date": "2026-09-04", "text": "Taxes paid 5,000"}
    monkeypatch.setattr(documents, "search", lambda chat_id, q, **kw: [])
    monkeypatch.setattr(documents, "local_only_match", lambda chat_id, q: doc if "itr" in q.lower() else None)
    monkeypatch.setattr(router, "provider_is_local", lambda p: False)

    class R: text = "You paid 5,000."

    async def fake_acall(**kw): return R()
    monkeypatch.setattr(router, "acall", fake_acall)
    secrets._last_private_doc.clear()
    out = asyncio.run(loop_tools._documents_search(1, {"query": "ITR taxes paid"}, "how much tax did I pay per the ITR?"))
    assert out == {"__direct_reply__": "🔒 You paid 5,000."}


def test_private_lane_follows_the_privacy_flag(monkeypatch):
    from kyraan.control_plane import config
    from kyraan.model_router import router
    from kyraan.agents import orchestrator
    base = {"model_tiers": {"frontier": {"provider": "openai", "model": "gpt-5.4-mini"},
                            "standby": {"provider": "openai", "model": "gpt-5.4-nano"},
                            "cheap": {"provider": "openai", "model": "gpt-5.4-nano"}},
            "privacy": {"private_on_standby": True}}
    monkeypatch.setattr(config, "load", lambda: base)
    monkeypatch.setattr(router, "provider_is_local", lambda p: p == "ollama")
    assert router.tier_may_see_private("cheap") and router.tier_may_see_private("standby")
    assert not router.tier_may_see_private("frontier")
    assert orchestrator.tier_chain() == ("frontier", "standby")        # same model listed once
    base["privacy"]["private_on_standby"] = False
    assert not router.tier_may_see_private("cheap")
    base["model_tiers"]["cheap"] = {"provider": "ollama", "model": "qwen3:8b"}
    assert router.tier_may_see_private("cheap")                        # a local endpoint always may
