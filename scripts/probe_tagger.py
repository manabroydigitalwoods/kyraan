"""Sensitivity-tagger probe: can a lighter local model tag episodes as
precisely as the resident cheap tier (qwen3:8b)?

Scoring is asymmetric on purpose: a MISS (expected flag absent) is the
dangerous direction — an untagged sensitive episode escapes the
discretion rules; over-tagging only costs a little recall. A candidate
is acceptable only with ZERO misses on the 'sensitive-ish' rows.

    .venv/bin/python scripts/probe_tagger.py [model ...]
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.store.episodes import _TAG_SYSTEM  # noqa: E402 — the REAL prompt
from kyraan.store.episodes import normalize_flags  # noqa: E402 — and filter

CANDIDATES = ["qwen3:8b", "llama3.2:latest", "qwen3:1.7b", "gemma3:1b"]

# (snippet, expected flags). Kyraan-shaped: reminders, family, home,
# email chatter — plus the clear positives each flag exists for.
PROBES = [
    ("user: remind me to drink water every hour from 10 to 9", set()),
    ("user: how's traffic to Jalpaiguri right now", set()),
    ("user: turn off the AC\nassistant: Done — the AC is off.", set()),
    ("user: what's the weather in Kolkata", set()),
    ("user: any new emails?\nassistant: [showed the unread email summary]", set()),
    ("user: my chest has been hurting since morning, should I worry", {"health"}),
    ("user: I got a burn on the stove, it's blistering", {"health", "safety"}),
    ("user: the gas smell in the kitchen is back again", {"safety"}),
    ("user: dad and I had a huge fight, we're not talking", {"emotional"}),
    ("user: I've been crying since the call about grandma", {"emotional"}),
    ("user: my salary got delayed again and the EMI bounces tomorrow",
     {"sensitive"}),
    ("user: the lawyer says we should settle the property case", {"sensitive"}),
    ("user: don't tell anyone but we're planning to move cities", {"sensitive"}),
    ("user: doctor increased my BP medicine dose", {"health"}),
    ("user: what did we discuss about hospitals near Siliguri", set()),
    ("user: set a reminder for Kiaan's vaccination on Friday", {"health"}),
]


def _tag(model: str, text: str) -> list:
    if model == "nano":  # the frontier tier through the real router
        from kyraan.model_router import router
        response = router.call(prompt=text, system=_TAG_SYSTEM,
                               tier="frontier", force_json=True, max_tokens=128)
        return json.loads(response.text).get("flags") or []
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps({
            "model": model, "stream": False, "format": "json",
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": _TAG_SYSTEM},
                         {"role": "user", "content": text}],
            "think": False,
        }).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read())
    return json.loads(payload["message"]["content"]).get("flags") or []


def probe(model: str) -> dict | None:
    misses = overs = exact = 0
    latencies = []
    try:
        _tag(model, "warmup")  # load off the clock
    except Exception as exc:
        print(f"  ❌ {model}: {exc}")
        return None
    for text, expected in PROBES:
        t0 = time.perf_counter()
        try:
            got = set(normalize_flags(_tag(model, text)))  # as production does
        except Exception as exc:
            print(f"  ❌ parse/call failed on {text[:40]!r}: {exc}")
            got = set()
        latencies.append(time.perf_counter() - t0)
        missed = expected - got
        extra = got - expected
        misses += bool(missed)
        overs += bool(extra)
        exact += got == expected
        mark = "❌MISS" if missed else ("➕over" if extra else "✅")
        print(f"  {mark:6s} {text[:52]!r:56s} want={sorted(expected)} got={sorted(got)}")
    avg_ms = sum(latencies) / len(latencies) * 1000
    print(f"  → {model}: {exact}/{len(PROBES)} exact, "
          f"{misses} MISSES (must be 0), {overs} over-tags, {avg_ms:.0f}ms/snippet")
    return {"model": model, "exact": exact, "misses": misses,
            "overs": overs, "ms": avg_ms}


def main() -> int:
    models = sys.argv[1:] or CANDIDATES
    results = []
    for model in models:
        print(f"\n{model}:")
        r = probe(model)
        if r:
            results.append(r)
    safe = [r for r in results if r["misses"] == 0]
    if not safe:
        print("\nNO candidate is miss-free — keep the baseline")
        return 1
    best = max(safe, key=lambda r: (r["exact"], -r["ms"]))
    print(f"\nMISS-FREE: {', '.join(r['model'] for r in safe)}")
    print(f"BEST: {best['model']} ({best['exact']}/{len(PROBES)} exact, "
          f"{best['overs']} over-tags, {best['ms']:.0f}ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
