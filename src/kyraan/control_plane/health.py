"""The doctor (owner: anomaly detection, 2026-08-27): one function that
answers "is everything working, and what needs attention?" — live
component probes plus a 24h anomaly census over events.jsonl, with
calibrated thresholds. Shared by scripts/health_check.py, the "health
report" chat phrase, and the nightly job.
"""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from kyraan.control_plane import logging_setup
from kyraan.control_plane.dnd import local_now

# census kinds → WARN threshold per 24h (None = any occurrence WARNs)
_CENSUS_WARN = {
    "agent_all_tiers_failed": None,
    "handle_message_error": None,
    "budget_exhausted": None,
    "memory_visibility_failclosed": None,
    "pg_mirror_stale": None,
    "model_call_error": 5,
    "agent_tier_fallback": 5,
    "agent_false_success_corrected": 5,
    "agent_deflection_corrected": 8,
    "extraction_skipped_slow": 5,
    "fact_sync_deferred": 3,
    "promise_sync_deferred": 3,
    "session_backend_fallback": 1,
    "memory_backend_fallback": 5,
    "promises_backend_fallback": 5,
    "episode_tagging_failed": 3,
    "triple_extract_deferred": 3,
    "nightly_stage_failed": None,
}


def _probe_components() -> list:
    """[(name, 'OK'|'FAIL', detail)] — live, each probe fail-isolated."""
    results = []

    def probe(name, fn):
        try:
            ok, detail = fn()
            results.append((name, "OK" if ok else "FAIL", detail))
        except Exception as exc:
            results.append((name, "FAIL", str(exc)[:80]))

    def _pg():
        from kyraan.store import pg
        return pg.available(), "container reachable"

    def _redis():
        from kyraan.store import redis_kv
        import redis as _r
        _r.Redis.from_url(redis_kv.url(), socket_connect_timeout=2).ping()
        return True, "ping ok"

    def _ollama():
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags",
                                    timeout=3) as resp:
            names = {m["name"] for m in json.loads(resp.read())["models"]}
        needed = {"qwen3:8b", "llama3.2:latest", "all-minilm:latest"}
        missing = needed - names
        return not missing, ("all models present" if not missing
                             else f"missing: {', '.join(sorted(missing))}")

    def _embedder():
        from kyraan.store import embed
        return embed.available(), f"{embed.EMBED_MODEL} {embed.EMBED_DIM}-d"

    def _searxng():
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8888/search?q=test&format=json",
            headers={"User-Agent": "kyraan/health"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        n = len(data.get("results", []))
        dead = [e[0] for e in data.get("unresponsive_engines", [])]
        return n > 0, (f"{n} results" + (f"; unresponsive: {', '.join(dead)}"
                                         if dead else ""))

    def _openai():
        import os
        if not os.environ.get("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY not set in this process"
        return True, "key present (usage evidence in census)"

    probe("postgres", _pg)
    probe("redis", _redis)
    probe("ollama models", _ollama)
    probe("embedder", _embedder)
    probe("searxng", _searxng)
    probe("openai key", _openai)
    def _wake():
        from kyraan.control_plane import wake
        # sudo readiness only — WARN-class: without the pmset rule
        # Kyraan degrades to late-but-honest delivery, nothing is lost.
        if wake.sudo_ready():
            return True, "pmset wake scheduling armed"
        # Degraded, not down: without the rule delivery is late-after-
        # sleep (the misfire fix), so this stays an OK with a loud
        # detail rather than failing the whole nightly verdict.
        return True, ("⚠ NOT armed — sudo rule missing, reminders fire "
                      "LATE after sleep; install /etc/sudoers.d/"
                      "kyraan-pmset (one-liner in control_plane/wake.py)")
    probe("wake planner", _wake)
    return results


def _census_24h() -> Counter:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    counts: Counter = Counter()
    files = [logging_setup.EVENT_LOG]
    archive = getattr(logging_setup, "ARCHIVE_DIR", None)
    if archive and archive.exists():
        files += sorted(archive.glob("*/events-*.jsonl"))[-2:]   # rotated names carry a stamp
    for path in files:
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("ts", "") >= cutoff:
                kind = event.get("kind", "")
                if kind in logging_setup.ANOMALY_KINDS or kind == "turn_health":
                    counts[kind] += 1
                    if kind == "turn_health" and event.get("anomaly_count"):
                        counts["_anomalous_turns"] += 1
    return counts


def report(probed: list | None = None) -> tuple:
    """(verdict, text): verdict 'OK'|'WARN'|'FAIL', text = the full
    human-readable report ending in the needs-work list.

    `probed` accepts an already-run component sweep. The probes make real
    network calls (searxng alone waits up to 8s), so a caller that wants
    BOTH the structured component list and this text — the web panel's
    system console does — passes its own sweep rather than paying for a
    second one.
    """
    lines, needs_work = [], []
    components = _probe_components() if probed is None else probed
    lines.append("COMPONENTS:")
    for name, status, detail in components:
        lines.append(f"  {'✅' if status == 'OK' else '❌'} {name}: {detail}")
        if status != "OK":
            needs_work.append(f"{name} is down ({detail})")
    census = _census_24h()
    turns = census.get("turn_health", 0)
    dirty = census.get("_anomalous_turns", 0)
    lines.append(f"\nLAST 24H: {turns} turns, {dirty} with anomalies")
    warned = []
    for kind, threshold in _CENSUS_WARN.items():
        count = census.get(kind, 0)
        if count and (threshold is None or count >= threshold):
            warned.append((kind, count))
    for kind, count in sorted(warned, key=lambda x: -x[1]):
        lines.append(f"  ⚠️ {kind} ×{count}")
        needs_work.append(f"{kind} ×{count} in 24h")
    other = [(k, c) for k, c in census.items()
             if k in logging_setup.ANOMALY_KINDS and (k, c) not in warned and c]
    if other:
        lines.append("  (below thresholds: "
                     + ", ".join(f"{k}×{c}" for k, c in sorted(other)) + ")")
    if needs_work:
        lines.append("\nNEEDS WORK:")
        lines += [f"  • {item}" for item in needs_work]
    else:
        lines.append("\nAll components up, all rates under thresholds.")
    component_fail = any(s != "OK" for _, s, _ in components)
    verdict = "FAIL" if component_fail else ("WARN" if warned else "OK")
    return verdict, "\n".join(lines)
