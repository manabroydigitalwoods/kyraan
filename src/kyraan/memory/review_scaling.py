"""Review scaling per governance §6 (P3.5e): full review of every
proposal until trust is EARNED — 200 total human reviews with a
trailing-50 approval rate >= 90%. Then `sampled` mode: every 3rd
proposal still holds for human review; the rest carry a 24h objection
window and auto-approve when it passes unobjected (they sit in the
pending queue, visible as "awaiting", exactly as today — a reject
during the window is the objection).

Full review RE-triggers, with the trailing window reset so trust is
re-earned, on any of:
- a wrong auto-approval (an auto-approved fact later forgotten),
- a model-tier change (fingerprint of both tiers),
- an extraction-prompt change (hash of the extraction system prompt).
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kyraan.control_plane.filelock import atomic_write_text, locked
from kyraan.control_plane.logging_setup import log_event

STATS_PATH = Path(__file__).resolve().parents[3] / "data" / "review_stats.json"

TOTAL_NEEDED = 200
RATE_NEEDED = 0.90
TRAILING = 50
HOLD_EVERY = 3
OBJECTION_HOURS = 24
_AUTO_RECENT_MAX = 50


def _load() -> dict:
    try:
        return json.loads(STATS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(stats: dict) -> None:
    STATS_PATH.parent.mkdir(exist_ok=True)
    atomic_write_text(STATS_PATH, json.dumps(stats, indent=1))


def _fingerprints() -> dict:
    from kyraan.control_plane import config
    from kyraan.memory.extraction import _EXTRACT_FACTS_SYSTEM
    tiers = config.load().get("model_tiers", {})
    tier_fp = "|".join(f"{t.get('provider')}:{t.get('model')}"
                       for _, t in sorted(tiers.items()))
    prompt_fp = hashlib.sha1(_EXTRACT_FACTS_SYSTEM.encode()).hexdigest()[:12]
    return {"tiers": tier_fp, "prompt": prompt_fp}


def record_decision(approved: bool) -> None:
    """One HUMAN review decision (auto-approvals never count)."""
    with locked(STATS_PATH):
        stats = _load()
        stats["total_reviewed"] = int(stats.get("total_reviewed", 0)) + 1
        recent = list(stats.get("recent", []))
        recent.append(1 if approved else 0)
        stats["recent"] = recent[-TRAILING:]
        _save(stats)


def retrigger(reason: str) -> None:
    """Back to full review; the trailing window resets so the 90% rate
    must be re-earned over 50 fresh human decisions."""
    with locked(STATS_PATH):
        stats = _load()
        stats["recent"] = []
        stats["since_hold"] = 0
        stats.pop("mode_logged", None)
        _save(stats)
    log_event("review_scaling_retrigger", reason=reason)


def review_mode() -> str:
    """'full' or 'sampled' — evaluated from the counters, with the tier
    and extraction-prompt fingerprints checked on every call (a change
    retriggers before the mode is answered)."""
    current = _fingerprints()
    with locked(STATS_PATH):
        stats = _load()
        stored = stats.get("fingerprints")
        if stored is not None and stored != current:
            changed = [k for k in current if stored.get(k) != current[k]]
            stats["recent"] = []
            stats["since_hold"] = 0
            stats["fingerprints"] = current
            _save(stats)
            log_event("review_scaling_retrigger",
                      reason=f"fingerprint changed: {','.join(changed)}")
            return "full"
        if stored is None:
            stats["fingerprints"] = current
            _save(stats)
        total = int(stats.get("total_reviewed", 0))
        recent = list(stats.get("recent", []))
        sampled = (total >= TOTAL_NEEDED and len(recent) >= TRAILING
                   and sum(recent) / len(recent) >= RATE_NEEDED)
        mode = "sampled" if sampled else "full"
        if stats.get("mode_logged") != mode:
            stats["mode_logged"] = mode
            _save(stats)
            log_event("review_scaling_mode", mode=mode, total=total,
                      rate=round(sum(recent) / len(recent), 3) if recent else 0)
        return mode


def next_proposal_holds() -> bool:
    """In sampled mode: True for every HOLD_EVERY-th proposal (held for
    human review); False = eligible for the 24h auto-approve window.
    In full mode: always True."""
    if review_mode() == "full":
        return True
    with locked(STATS_PATH):
        stats = _load()
        count = int(stats.get("since_hold", 0)) + 1
        if count >= HOLD_EVERY:
            stats["since_hold"] = 0
            _save(stats)
            return True
        stats["since_hold"] = count
        _save(stats)
        return False


def objection_deadline() -> str:
    return (datetime.now(timezone.utc)
            + timedelta(hours=OBJECTION_HOURS)).isoformat()


def record_auto_approved(content: str) -> None:
    digest = hashlib.sha1(content.strip().encode()).hexdigest()[:16]
    with locked(STATS_PATH):
        stats = _load()
        recent = list(stats.get("auto_recent", []))
        recent.append(digest)
        stats["auto_recent"] = recent[-_AUTO_RECENT_MAX:]
        _save(stats)


def on_forgotten(contents: list) -> None:
    """A recently auto-approved fact being forgotten = a WRONG
    auto-approval — the §6 retrigger."""
    digests = {hashlib.sha1(c.strip().encode()).hexdigest()[:16]
               for c in contents}
    stats = _load()
    if digests & set(stats.get("auto_recent", [])):
        retrigger("wrong auto-approval (auto-approved fact forgotten)")


def sweep_auto_approvals() -> int:
    """Promote pending proposals whose unobjected 24h window has passed.
    Auto-approvals never touch the human counters."""
    from kyraan.memory import store as memory_store
    now = datetime.now(timezone.utc).isoformat()
    promoted = 0
    for path in sorted(memory_store.PENDING_DIR.glob("*.md")):
        text = path.read_text()
        deadline = next((line.split("auto_approve_after:", 1)[1].strip()
                         for line in text.splitlines()
                         if line.startswith("auto_approve_after:")), None)
        if not deadline or deadline > now:
            continue
        _, _, rest = text.partition("---\n")
        _, _, body = rest.partition("---\n")
        try:
            memory_store.promote(path, human=False)
        except Exception as exc:
            log_event("auto_approve_failed", path=path.name,
                      error=str(exc)[:120])
            continue
        record_auto_approved(body.strip().lstrip("- ").strip())
        promoted += 1
        log_event("memory_auto_approved", path=path.name)
    return promoted
