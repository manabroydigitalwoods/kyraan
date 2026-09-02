"""Entity extraction (local tier) and the brain's core node."""
import json

import pytest

from kyraan.store import entities


class _R:
    def __init__(self, text): self.text = text


def test_extract_accepts_only_what_the_text_contains(monkeypatch):
    from kyraan.model_router import router
    monkeypatch.setattr(router, "call", lambda **kw: _R(json.dumps({
        "entities": ["Carbamide Forte", "Omefish-Ultra", "Made Up Brand"],
        "category": "#supplement"})))
    monkeypatch.setattr(router, "strip_code_fence", lambda t: t)
    out = entities.extract("Carbamide Forte Omefish-Ultra 5x Strength Omega-3, 60 caps",
                           hint="my supplement")
    assert out == ["Carbamide Forte", "Omefish-Ultra", "#supplement"]   # invention dropped


def test_extract_is_contained_on_model_failure(monkeypatch):
    from kyraan.model_router import router
    def boom(**kw): raise RuntimeError("ollama down")
    monkeypatch.setattr(router, "call", boom)
    assert entities.extract("Some receipt text here") == []
    assert entities.extract("x") == []                                 # too thin


def test_extract_uses_the_local_tier_only(monkeypatch):
    from kyraan.model_router import router
    tiers = []
    def call(**kw):
        tiers.append(kw.get("tier")); return _R('{"entities": [], "category": ""}')
    monkeypatch.setattr(router, "call", call)
    monkeypatch.setattr(router, "strip_code_fence", lambda t: t)
    entities.extract("HP Gas cash memo Rs 1150 dated 2 Sep")
    assert tiers == ["cheap"]


def test_brain_has_kyraan_at_the_core(monkeypatch):
    from kyraan.panel import queries
    g = queries.brain_graph(fresh=True)
    core = next((n for n in g["nodes"] if n["id"] == "k:kyraan"), None)
    assert core and core["type"] == "core"
    core_edges = [e for e in g["edges"] if e["a"] == "k:kyraan"]
    assert core_edges                                    # wired to skills/tasks/owner
    assert {e["kind"] for e in core_edges} <= {"acts", "talks"}
