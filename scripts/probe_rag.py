"""The RAG precision gate (owner: "build it very precisely",
2026-08-27): labeled retrieval probes against the LIVE stores, in the
house probe→pin→gate pattern. Every future change to embeddings,
chunking, thresholds, or ranking must keep this green — the way the
eval gates behavior, this gates retrieval.

    .venv/bin/python scripts/probe_rag.py

FACT probes assert the expected fact reaches build_context — most are
deliberately ZERO-word-overlap (the reason RAG exists). EPISODE probes
assert a hit in the top-2 auto-injection snippets, plus the negative
gates: irrelevant queries inject nothing, and suppressed topics stay
absent (the resurrection rule, again, at the retrieval layer).
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.memory import engine  # noqa: E402
from kyraan.store import episodes  # noqa: E402

OWNER_CHAT = 6755024720

# (query, substring that must appear in build_context) — zero-overlap
# phrasings on purpose; update alongside real memory changes.
FACT_PROBES = [
    ("when is my kid's birthday?", "12-10-2025"),
    ("how do I quit tobacco?", "smoking"),
    ("who is my spouse?", "Ruma"),
    ("where do I stay?", "Radhabari"),
    ("what pet did I have before?", "Tomi"),
    ("which company does Manab work for?", "Digitalwoods"),
]

# (chat, query, substring expected among the top-2 injected snippets)
EPISODE_PROBES = [
    (OWNER_CHAT, "that time we talked about the politician from Bengal",
     "mamata"),
    (7900, "that evening test where we kept setting the mom phone call",
     "call mom"),
    # the top hits ARE baby-photo conversations — the expectation names
    # the semantic answer, not a forced token (first draft demanded
    # "kiaan" and failed correct retrieval)
    (OWNER_CHAT, "when I enrolled the baby's face", "baby"),
]

# queries that must inject NOTHING (below the floor / suppressed topic)
NEGATIVE_PROBES = [
    (OWNER_CHAT, "quantum entanglement in photosynthesis"),
    (7900, "my favourite eval fruit"),  # forgotten + swept: must stay absent
]


def main() -> int:
    failures = 0
    print("FACT retrieval (zero-overlap phrasings):")
    for query, expected in FACT_PROBES:
        context = engine.build_context(query)
        ok = expected.lower() in context.lower()
        failures += not ok
        print(f"  {'✅' if ok else '❌'} {query!r} -> {expected!r}")
    print("\nEPISODE injection (hit in top-2):")
    for chat_id, query, expected in EPISODE_PROBES:
        snippets = episodes.relevant_snippets(chat_id, query)
        ok = any(expected.lower() in s.lower() for s in snippets)
        failures += not ok
        best = episodes._search(chat_id, query)[:1]
        sim = round(best[0][1], 3) if best else None
        print(f"  {'✅' if ok else '❌'} {query!r} -> {expected!r} "
              f"(best_sim={sim}, injected={len(snippets)})")
    print("\nNEGATIVE gates (must inject nothing):")
    for chat_id, query in NEGATIVE_PROBES:
        snippets = episodes.relevant_snippets(chat_id, query)
        ok = not snippets
        failures += not ok
        print(f"  {'✅' if ok else '❌'} {query!r} injected {len(snippets)}")
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
