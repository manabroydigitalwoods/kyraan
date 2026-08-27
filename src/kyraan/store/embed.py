"""Local embeddings for episodic recall (P3.3a) — Ollama /api/embed.

LOCAL-ONLY guarantee: episode text may include local_only content on
the write path, so this module refuses to run at all unless the
resolved Ollama endpoint is local — the SAME resolution routing uses
(router.provider_is_local; security round 2, P2: a second hand-rolled
locality check could disagree with the client's).

The model and dimension are PINNED here from the P3.3a probe
(scripts/probe_embedder.py, 2026-08-27 on this Mac):
qwen3-embedding:0.6b, 1024-d — 4/4 sanity gates with the best margin
(0.333 vs nomic's 0.319), 309ms for a 12-text batch, ~0.9s cold reload
(inside the reply-path budget; the scary 10.2s was first-ever load).
migrations/004_episodes.sql carries the same dimension — change one and
you must change both (test_embed pins them together).
"""
import json
import urllib.request

EMBED_MODEL = "qwen3-embedding:0.6b"
EMBED_DIM = 1024
_TIMEOUT_S = 30


class EmbedderNotLocal(RuntimeError):
    """The resolved Ollama endpoint is not on this machine."""


def _endpoint() -> str:
    from kyraan.control_plane import config
    from kyraan.model_router import router
    if not router.provider_is_local("ollama"):
        raise EmbedderNotLocal(
            "refusing to embed: the resolved Ollama endpoint is not local "
            "— episode text never leaves this machine")
    provider_cfg = (config.load().get("providers") or {}).get("ollama") or {}
    return router.resolve_base_url("ollama", provider_cfg).rstrip("/")


def embed(texts: list) -> list:
    """Embed a batch of texts → list of EMBED_DIM float vectors, same
    order. Raises on any failure — callers decide their degradation."""
    if not texts:
        return []
    request = urllib.request.Request(
        f"{_endpoint()}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": list(texts)}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as resp:
        payload = json.loads(resp.read())
    vectors = payload.get("embeddings") or []
    if len(vectors) != len(texts):
        raise RuntimeError(
            f"embedder returned {len(vectors)} vectors for {len(texts)} texts")
    for v in vectors:
        if len(v) != EMBED_DIM:
            raise RuntimeError(
                f"embedder returned {len(v)}-d, pinned {EMBED_DIM}-d — "
                "model changed under us; fix EMBED_MODEL/EMBED_DIM + migration")
    return vectors


def available() -> bool:
    """Cheap probe: local endpoint reachable and the pinned model loaded."""
    try:
        return len(embed(["probe"])[0]) == EMBED_DIM
    except Exception:
        return False


def cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
