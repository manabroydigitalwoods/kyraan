"""Cross-person contradiction DETECTION (owner: "fix it", 2026-08-27 —
the multi-user audit's weakest link). P3.5d handles conflicts declared
through supersession; this scan finds the undeclared ones: two people
stating facts that cannot both be true, neither ever superseding.

House pattern: the frontier model PROPOSES conflicting pairs, a
deterministic validator keeps only pairs whose facts exist, are active,
and have DIFFERENT authors (authorship is never the model's call), and
applying is exactly the P3.5d dispute state — both facts stand flagged,
a resolvable notice lands in the subject-owner's queue, and the
existing approve/reject semantics settle it under that reviewer's own
authority. Nothing is deleted by detection, ever.
"""
import json

from kyraan.control_plane.logging_setup import log_event
from kyraan.memory import engine

_SCAN_SYSTEM = """You review a person's saved memory facts for CONTRADICTIONS —
pairs that cannot BOTH be true (different values for the same attribute
of the same person or thing: two birthdays, two current schools, two
phone numbers for the same person, incompatible schedules).
Reply ONLY with JSON:
{"conflicts": [{"a": "<id>", "b": "<id>", "reason": "<short>"}]}
Rules:
- A contradiction is about the SAME attribute. Different facts about the
  same person (a name AND a birthday) are NOT conflicts.
- Restatements or more/less precise versions of the SAME claim are NOT
  conflicts (they are duplicates, handled elsewhere).
- When unsure, it is not a conflict. Empty list is a fine answer.
No other keys, no prose."""


def scan() -> list:
    """Frontier-proposed, deterministically validated conflict pairs:
    [{'earlier': entry, 'later': entry, 'reason': str}] — earlier/later
    decided by created timestamps, never by the model."""
    from kyraan.model_router import router
    entries = {e["id"]: e for e in engine.active_entries()}
    if len(entries) < 2:
        return []
    listing = "\n".join(
        f'{e["id"]} [{e.get("author", "owner")}]: {e["content"]}'
        for e in entries.values())
    response = router.call(prompt=listing, system=_SCAN_SYSTEM,
                           tier="frontier", force_json=True, max_tokens=1024)
    raw = json.loads(router.strip_code_fence(response.text)).get("conflicts") or []
    pairs, claimed = [], set()
    for item in raw:
        a, b = str(item.get("a", "")), str(item.get("b", ""))
        if (a not in entries or b not in entries or a == b
                or (a, b) in claimed or (b, a) in claimed):
            continue
        ea, eb = entries[a], entries[b]
        if ea.get("author", "owner") == eb.get("author", "owner"):
            continue  # same-person contradictions are supersession/dedup
        if ("disputed" in (ea.get("flags") or [])
                and "disputed" in (eb.get("flags") or [])):
            continue  # already flagged — the notice exists
        claimed.update({(a, b)})
        earlier, later = sorted((ea, eb), key=lambda e: e.get("created", ""))
        pairs.append({"earlier": earlier, "later": later,
                      "reason": str(item.get("reason", ""))[:200]})
    log_event("conflict_scan", active=len(entries), conflicts=len(pairs))
    return pairs


def apply(pairs: list) -> int:
    """Flag each validated pair and file the resolvable notice — the
    same dispute state supersession-time conflicts produce."""
    from kyraan.memory import store as memory_store
    filed = 0
    for pair in pairs:
        earlier, later = pair["earlier"], pair["later"]
        engine.flag_disputed(earlier["id"], later["id"])
        try:
            memory_store.file_dispute(
                target=later["target"],
                reviewer=engine._subject_owner_for(later["target"]),
                old_id=earlier["id"], new_id=later["id"],
                old_content=earlier["content"], new_content=later["content"])
            filed += 1
            log_event("memory_conflict_flagged",
                      earlier=earlier["content"][:80],
                      later=later["content"][:80], reason=pair["reason"])
        except Exception as exc:
            log_event("dispute_notice_failed", reason=str(exc)[:150])
    return filed


def nightly_scan() -> int:
    """Scan + apply; returns notices filed. Any failure is logged and
    contained — detection must never break the night."""
    try:
        return apply(scan())
    except Exception as exc:
        log_event("conflict_scan_failed", reason=str(exc)[:150])
        return 0
