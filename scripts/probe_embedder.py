"""P3.3a embedder probe: run the candidate models on this Mac, check the
similarity sanity gates, report latency — the winner gets pinned in
store/embed.py + migrations/004_episodes.sql.

    .venv/bin/python scripts/probe_embedder.py
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.store.embed import cosine  # noqa: E402

CANDIDATES = ["all-minilm", "snowflake-arctic-embed:22m",
              "nomic-embed-text", "qwen3-embedding:0.6b"]

# The ticket's gate plus checks shaped like real Kyraan recall traffic.
SANITY = [  # (a, b, c) — expect sim(a,b) > sim(a,c)
    ("cat", "kitten", "carburetor"),
    ("remind me to drink water every hour",
     "hourly hydration reminder schedule",
     "the calendar has a meeting at 3pm"),
    ("my son Kiaan was born in October",
     "Kiaan's birthday and age",
     "traffic between Siliguri and Jalpaiguri"),
    ("what did we discuss about the smart home AC",
     "turning the air conditioner on and off",
     "favourite snack is murukku"),
]


def _embed(model: str, texts: list) -> list:
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/embed",
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["embeddings"]


def probe(model: str) -> dict | None:
    texts = sorted({t for triple in SANITY for t in triple})
    try:
        t0 = time.perf_counter()
        _embed(model, ["warmup"])  # load the model off the clock
        load_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        vectors = dict(zip(texts, _embed(model, texts)))
        batch_ms = (time.perf_counter() - t0) * 1000
    except Exception as exc:
        print(f"❌ {model}: {exc}")
        return None
    passes, margins = 0, []
    for a, b, c in SANITY:
        close = cosine(vectors[a], vectors[b])
        far = cosine(vectors[a], vectors[c])
        ok = close > far
        passes += ok
        margins.append(close - far)
        print(f"  {'✅' if ok else '❌'} {a[:38]!r}: near={close:.3f} far={far:.3f}")
    dim = len(next(iter(vectors.values())))
    print(f"  → {model}: {passes}/{len(SANITY)} gates, {dim}-d, "
          f"load {load_s:.1f}s, batch of {len(texts)} in {batch_ms:.0f}ms, "
          f"avg margin {sum(margins)/len(margins):.3f}")
    return {"model": model, "passes": passes, "dim": dim, "load_s": load_s,
            "margin": sum(margins) / len(margins), "batch_ms": batch_ms}


# Recall (P3.3c) embeds the QUERY on the reply path; a model that takes
# many seconds to cold-load repeats the qwen3:8b 23s-reload incident
# every time Ollama evicts it. Full gate passes are mandatory; among
# passers, a reply-path-safe load beats a small margin edge.
_LOAD_BUDGET_S = 2.0


def main() -> int:
    results = []
    for model in CANDIDATES:
        print(f"\n{model}:")
        result = probe(model)
        if result:
            results.append(result)
    full = [r for r in results if r["passes"] == len(SANITY)]
    if not full:
        print("\nNO candidate passed every gate")
        return 1
    safe = [r for r in full if r["load_s"] <= _LOAD_BUDGET_S]
    best = max(safe or full, key=lambda r: r["margin"])
    slow = [r["model"] for r in full if r not in safe]
    if safe and slow:
        print(f"\n(reply-path rule: {', '.join(slow)} passed but cold-loads "
              f"over {_LOAD_BUDGET_S:.0f}s — excluded)")
    print(f"\nWINNER: {best['model']} ({best['dim']}-d) — pin this in "
          "store/embed.py and migrations/004_episodes.sql")
    return 0


if __name__ == "__main__":
    sys.exit(main())
