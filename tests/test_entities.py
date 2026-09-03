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
    # nothing invented — but the deterministic category still lands
    assert entities.extract("Some receipt text here") == ["#receipt"]
    assert entities.extract("Some plain text with no family") == []
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
    kinds = {e["kind"] for e in core_edges}
    assert kinds <= {"acts", "fires", "received", "talks"}
    docs = [n["id"] for n in g["nodes"] if n["type"] in ("document", "note")]
    received = {e["b"] for e in core_edges if e["kind"] == "received"}
    assert set(docs) <= received                         # no orphan documents


def test_category_fallback_when_the_model_offers_none(monkeypatch):
    from kyraan.model_router import router
    monkeypatch.setattr(router, "call", lambda **kw: _R('{"entities": [], "category": ""}'))
    monkeypatch.setattr(router, "strip_code_fence", lambda t: t)
    assert entities.extract("HP Gas cash memo Rs 1150", hint="Cash Memo") == ["#receipt"]
    assert entities.category_from_words("Boarding pass PNR 4X7Y") == "#ticket"
    assert entities.category_from_words("a sunny afternoon") == ""


def test_generic_entities_are_refused(monkeypatch):
    from kyraan.model_router import router
    monkeypatch.setattr(router, "call", lambda **kw: _R(json.dumps({
        "entities": ["photo", "Payment Status", "Sharma Medical"], "category": "#photo"})))
    monkeypatch.setattr(router, "strip_code_fence", lambda t: t)
    out = entities.extract("photo of the Payment Status screen at Sharma Medical shop")
    assert out == ["Sharma Medical", "#receipt"]    # generics dropped, #photo refused, fallback category


def test_clean_splits_glued_tags_and_keeps_one_hub():
    """Live 2026-09-03: the vision model returned 'Wellbeing Nutrition
    #health', '#Plant Based #nutrition' and photo.py stored them raw."""
    text = "Wellbeing Nutrition melts into throat relief lozenges, plant based, sugar free"
    out = entities.clean(["Wellbeing Nutrition #health",
                          "melts into throat relief #throat-relief",
                          "#Plant Based #nutrition", "#Sugar Free #nutrition"],
                         text, hint="my medicine")
    assert out == ["Wellbeing Nutrition", "melts into throat relief", "#medical"]
    # no family in the words -> the model's first valid tag names it
    assert entities.clean(["Acme #gadget"], "Acme thing") == ["Acme", "#gadget"]
    # invention and generic words never pass
    assert entities.clean(["Made Up", "Address"], "Address line") == []


def test_a_face_links_to_the_person_through_its_display_name(monkeypatch):
    """Owner 2026-09-03: the person was created from the face and the
    brain still showed the face unlinked — resolution used the file slug
    ("akansha-employee"), not the display name that became the alias."""
    from kyraan.panel import queries
    src = open(queries.__file__).read()
    assert '_person_node(entry["label"]) or _person_node(entry["id"][2:])' in src
