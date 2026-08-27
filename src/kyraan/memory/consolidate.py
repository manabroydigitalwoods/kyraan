"""Semantic memory consolidation (gap audit 2026-08-27): the stores
dedup MECHANICALLY (exact content on promote, per-provenance uniqueness
in the graph) but nothing ever notices that "Son Kiaan was born around
October 2025" and "My son Kiaan was born on 12-10-2025" are the same
fact. This pass uses the frontier model to PROPOSE duplicate groups —
and only the owner's approval applies them, exactly like fact review.

Applying a group marks the duplicates superseded_by the kept fact (the
same mechanism a correction uses): they deactivate, stay as history,
their PG mirrors update, and their graph triples stop being served —
the read-side cascade already handles downstream. Nothing is deleted.
"""
import json

from kyraan.control_plane.logging_setup import log_event
from kyraan.memory import engine

_SCAN_SYSTEM = """You review a person's saved memory facts for SEMANTIC duplicates —
entries stating the SAME fact about the same person or thing, possibly with
different wording or precision. Reply ONLY with JSON:
{"groups": [{"keep": "<id>", "duplicates": ["<id>", ...], "reason": "<short>"}]}
Rules:
- Group ONLY true duplicates: same subject, same claim. Different facts about
  the same person (name vs birthday) are NOT duplicates.
- "keep" = the most PRECISE and complete statement (an exact date beats
  "around October"; a richer description beats a bare mention).
- A vaguer statement that the kept one fully implies IS a duplicate.
- CONFLICTING claims (different fathers' names) are NOT duplicates — never
  group them; disputes are a human call.
- No groups is a fine answer: {"groups": []}.
No other keys, no prose."""


def scan() -> list:
    """Ask the frontier model for duplicate groups over ACTIVE facts.
    Returns validated proposals: [{keep, keep_content, duplicates:
    [(id, content)...], reason}]. Invalid ids/overlaps are dropped."""
    from kyraan.model_router import router
    entries = {e["id"]: e for e in engine.active_entries()}
    if len(entries) < 2:
        return []
    listing = "\n".join(f'{e["id"]}: {e["content"]}' for e in entries.values())
    response = router.call(prompt=listing, system=_SCAN_SYSTEM,
                           tier="frontier", force_json=True, max_tokens=1024)
    raw = json.loads(router.strip_code_fence(response.text)).get("groups") or []
    proposals, claimed = [], set()
    for group in raw:
        keep = str(group.get("keep", ""))
        dups = [str(d) for d in (group.get("duplicates") or [])]
        ids = [keep] + dups
        if (keep not in entries or not dups
                or any(d not in entries or d == keep for d in dups)
                or any(i in claimed for i in ids)):
            continue  # hallucinated ids, self-reference, or overlap
        claimed.update(ids)
        proposals.append({
            "keep": keep, "keep_content": entries[keep]["content"],
            "duplicates": [(d, entries[d]["content"]) for d in dups],
            "reason": str(group.get("reason", ""))[:200],
        })
    log_event("memory_consolidation_scan", active=len(entries),
              proposals=len(proposals))
    return proposals


def apply(keep_id: str, dup_ids: list) -> list:
    """Owner-approved: supersede the duplicates by the kept fact."""
    return engine.consolidate(keep_id, dup_ids)
