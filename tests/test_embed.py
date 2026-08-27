"""P3.3a — the embedder module: the dimension pin (module constant ==
migration DDL), the locality refusal, and — when local Ollama is up with
the pinned model — the live dimension + similarity sanity gate."""
import re
from pathlib import Path

import pytest

from kyraan.store import embed

_REPO = Path(__file__).resolve().parents[1]


def test_dimension_pin_matches_the_migration():
    ddl = (_REPO / "migrations" / "004_episodes.sql").read_text()
    match = re.search(r"vector\((\d+)\)", ddl)
    assert match, "episode DDL lost its vector column"
    assert int(match.group(1)) == embed.EMBED_DIM


def test_openai_wire_suffix_is_stripped(monkeypatch):
    # OLLAMA_BASE_URL=…/v1 (the chat client's base) must not leak into
    # the native /api/embed path — the live-404 regression.
    from kyraan.model_router import router
    monkeypatch.setattr(router, "provider_is_local", lambda name: True)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    assert embed._endpoint() == "http://localhost:11434"


def test_refuses_a_non_local_endpoint(monkeypatch):
    from kyraan.model_router import router
    monkeypatch.setattr(router, "provider_is_local", lambda name: False)
    with pytest.raises(embed.EmbedderNotLocal):
        embed.embed(["anything"])


def test_empty_batch_never_touches_the_network(monkeypatch):
    def boom(name):
        raise AssertionError("endpoint resolved for an empty batch")

    from kyraan.model_router import router
    monkeypatch.setattr(router, "provider_is_local", boom)
    assert embed.embed([]) == []


_LIVE = embed.available()


@pytest.mark.skipif(not _LIVE, reason="local Ollama embedder unreachable")
def test_live_dimension_and_similarity_sanity():
    vectors = embed.embed(["cat", "kitten", "carburetor"])
    assert all(len(v) == embed.EMBED_DIM for v in vectors)
    cat, kitten, carburetor = vectors
    assert embed.cosine(cat, kitten) > embed.cosine(cat, carburetor)


@pytest.mark.skipif(not _LIVE, reason="local Ollama embedder unreachable")
def test_live_batch_order_is_preserved():
    a, b = embed.embed(["water reminder", "water reminder"])
    assert embed.cosine(a, b) > 0.999  # identical texts, identical vectors
