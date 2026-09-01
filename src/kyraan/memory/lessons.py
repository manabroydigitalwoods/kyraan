"""Learned rules — the correction→behavior loop (owner "go",
2026-09-01; §3d gap #6, audit P1 "reflection").

A correction the owner repeats is a lesson the system keeps missing.
This module closes that loop with the SAME trust pattern facts use:
- CLUSTERING is deterministic (content-word overlap over the
  user_correction_candidate corpus the orchestrator already logs);
- a cluster of 3+ corrections across 2+ days earns ONE drafted rule —
  drafted on the LOCAL tier (corrections quote private text; the
  drafting prompt never leaves the machine);
- the draft goes to the owner's PENDING REVIEW queue like any fact,
  with its source corrections quoted as evidence;
- only the owner's yes lands it in data/learned_rules.json, rendered
  into every prompt's PERSONA block (capped) — a visible line in a
  file, never weights, never silent; retire undoes it.
"""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kyraan.control_plane.filelock import atomic_write_text, locked
from kyraan.control_plane.logging_setup import log_event

RULES_PATH = Path(__file__).resolve().parents[3] / "data" / "learned_rules.json"

MAX_ACTIVE = 10          # prompt budget: the persona block stays lean
_MIN_CLUSTER = 3         # corrections before a lesson is worth proposing
_MIN_DAYS = 2            # ... spanning at least this many distinct days
_LOOKBACK_DAYS = 14

_STOP = frozenset(
    "a an and are at be but can did do dont for i in is it its just me my "
    "no not of on or please said say that the this to was we you your "
    "wrong youre".split())


def _words(text: str) -> frozenset:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", text.lower())
                     if len(w) > 2 and w not in _STOP)


def _load() -> dict:
    try:
        return json.loads(RULES_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"rules": [], "seen": []}


def _save(data: dict) -> None:
    RULES_PATH.parent.mkdir(exist_ok=True)
    atomic_write_text(RULES_PATH, json.dumps(data, indent=1,
                                             ensure_ascii=False))


def active_rules() -> list:
    return [r for r in _load()["rules"] if r.get("active")][:MAX_ACTIVE]


def block() -> str:
    """The prompt lines — owner-approved lessons only."""
    rules = active_rules()
    if not rules:
        return ""
    return ("\nLEARNED RULES (each approved by the owner after repeated "
            "corrections — follow them):\n"
            + "\n".join(f"- {r['rule']}" for r in rules))


def apply(rule: str, sources: list) -> str:
    """The owner's yes: the rule goes live. Returns its id."""
    rule_id = uuid.uuid4().hex[:8]
    with locked(RULES_PATH):
        data = _load()
        data["rules"].append({
            "id": rule_id, "rule": rule.strip()[:200],
            "sources": [s[:160] for s in sources[:5]],
            "created": datetime.now(timezone.utc).isoformat(),
            "active": True})
        _save(data)
    log_event("learned_rule_adopted", rule_id=rule_id, rule=rule[:80])
    return rule_id


def retire(ref: str) -> dict:
    """Deactivate ONE rule by id-prefix or rule words — ambiguity
    refuses, like every other resolver here."""
    ref_l = ref.strip().lower()
    with locked(RULES_PATH):
        data = _load()
        live = [r for r in data["rules"] if r.get("active")]
        hits = [r for r in live if r["id"].startswith(ref_l)] or \
               [r for r in live if ref_l in r["rule"].lower()]
        if not hits:
            raise ValueError("no active learned rule matches — "
                             "say 'list learned rules' first")
        if len(hits) > 1:
            raise ValueError(f"{len(hits)} rules match — use more words")
        hits[0]["active"] = False
        _save(data)
    log_event("learned_rule_retired", rule_id=hits[0]["id"])
    return hits[0]


def _mark_seen(fingerprint: str) -> None:
    with locked(RULES_PATH):
        data = _load()
        if fingerprint not in data["seen"]:
            data["seen"].append(fingerprint)
            data["seen"] = data["seen"][-200:]
            _save(data)


def _corrections(now=None) -> list:
    """(day, text) for owner corrections in the lookback window, from
    the live event log and the archives."""
    from kyraan.control_plane import logging_setup as _logs
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    out = []
    paths = [_logs.EVENT_LOG]
    archive = getattr(_logs, "ARCHIVE_DIR", None)
    if archive and Path(archive).exists():
        paths += sorted(Path(archive).rglob("events-*.jsonl"))
    for path in paths:
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if '"user_correction_candidate"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("ts", "") >= cutoff and e.get("correction"):
                out.append((e["ts"][:10], e["correction"]))
    return out


def find_clusters(now=None) -> list:
    """Deterministic: greedy overlap clustering; a cluster earns a
    proposal at 3+ corrections over 2+ distinct days, once ever (the
    fingerprint survives approve AND reject)."""
    seen = set(_load()["seen"])
    clusters: list = []          # [(wordset, [(day, text)])]
    for day, text in _corrections(now):
        ws = _words(text)
        if len(ws) < 2:
            continue
        for cw, items in clusters:
            shared = len(ws & cw)
            if shared / min(len(ws), len(cw)) >= 0.5:
                items.append((day, text))
                break
        else:
            clusters.append((ws, [(day, text)]))
    ready = []
    for cw, items in clusters:
        if len(items) < _MIN_CLUSTER or len({d for d, _ in items}) < _MIN_DAYS:
            continue
        fingerprint = " ".join(sorted(cw)[:6])
        if fingerprint in seen:
            continue
        ready.append((fingerprint, [t for _, t in items]))
    return ready


async def scan_and_propose() -> int:
    """Nightly: cluster → draft (LOCAL tier only) → pending review.
    Returns proposals written. Every failure is contained — a bad
    night proposes nothing and the next night retries."""
    from kyraan.memory import store
    from kyraan.model_router import router
    written = 0
    for fingerprint, texts in find_clusters():
        prompt = (
            "The owner corrected the assistant with these messages, on "
            "different days:\n"
            + "\n".join(f"- {t}" for t in texts[:5])
            + "\n\nWrite ONE short standing behavior rule (max 120 chars, "
              "imperative, no preamble) that would prevent this class of "
              "correction. Reply with the rule text only.")
        try:
            response = await router.acall(prompt=prompt, system="",
                                          tier="cheap", max_tokens=200)
            rule = (response.text or "").strip().strip('"').splitlines()[0][:200]
        except Exception as exc:
            log_event("lesson_draft_failed", error=str(exc)[:100])
            continue
        if len(rule) < 8:
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", rule.lower())[:40].strip("_")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = store.PENDING_DIR / (
            f"{stamp}-{uuid.uuid4().hex[:6]}__persona__{slug}.md")
        evidence = "; ".join(t[:80] for t in texts[:3])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            f"target: persona/{slug}.md\n"
            f"source_statement: \"repeated corrections: {evidence[:300]}\"\n"
            "reviewer: owner\n"
            'meta: {"term": "long", "importance": "high", "era": "current", '
            '"sphere": "both", "flags": [], "supersedes": null, '
            '"kind": "learned_rule"}\n'
            "---\n\n"
            f"- LEARNED RULE (behavior, not a fact): {rule}\n")
        _mark_seen(fingerprint)
        written += 1
        log_event("lesson_proposed", rule=rule[:80],
                  corrections=len(texts))
    return written
