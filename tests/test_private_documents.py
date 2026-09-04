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
