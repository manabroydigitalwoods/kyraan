"""Entity extraction for stored text (owner 2026-09-02: "lots of
documents, no proper link"). A cash memo has no PERSON to link to — it
has a vendor, an amount, a category; those are the hubs documents
connect through. Runs on the LOCAL tier only (document text may be
private; nothing leaves the machine), returns short strings exactly as
written plus one #category, never people (they are subjects, registry-
bounded elsewhere). Deterministic guards on top: length caps, dedupe,
no guesses accepted beyond what the text contains.
"""
import json
import re

from kyraan.control_plane.logging_setup import log_event

_SYSTEM = (
    "Extract the named THINGS from this text: brand, vendor, shop, "
    "product, organisation, place, event, document type. Output ONLY a "
    "JSON object {\"entities\": [...], \"category\": \"#tag\"}: entities as "
    "short strings exactly as written in the text (no people's names), "
    "category as ONE lowercase #tag naming what the document IS "
    "(#receipt, #invoice, #medical, #supplement, #ticket, #card, "
    "#photo). Empty list when nothing is named. No explanations.")

_MAX = 10
# Generic words are not entities — a hub named "photo" joining every
# moment is noise, not a connection (backfill 2026-09-02 returned these).
_GENERIC = frozenset(
    "photo image picture document text watermark screenshot page file "
    "receipt invoice card label bottle product item payment status "
    "transaction txn id bank amount total date time online".split())
_GENERIC_TAGS = frozenset({"#photo", "#image", "#picture", "#document",
                           "#screenshot", "#text", "#file"})


def extract(text: str, hint: str = "") -> list:
    """[entities..., '#category'] from text — local tier, contained."""
    text = str(text or "").strip()
    if len(text) < 8:
        return []
    from kyraan.model_router import router
    prompt = (f"TITLE: {hint}\n" if hint else "") + f"TEXT:\n{text[:2500]}"
    try:
        resp = router.call(prompt=prompt, system=_SYSTEM, tier="cheap",
                           max_tokens=200, force_json=True)
        data = json.loads(router.strip_code_fence(resp.text or "{}"))
    except Exception as exc:
        log_event("entity_extract_failed", error=str(exc)[:100])
        return []
    low = text.lower()
    cat = str(data.get("category") or "").strip().lower()
    out = []
    for raw in (data.get("entities") or []):
        item = " ".join(str(raw).split())[:60]
        words = item.lower().split()
        # accept only what the text actually contains — no invention —
        # and never a generic word (or a phrase made only of them)
        if (len(item) >= 2 and item.lower() in low and item not in out
                and not all(w in _GENERIC for w in words)
                and item.lower() != cat.lstrip("#")):
            out.append(item)
        if len(out) >= _MAX:
            break
    if re.fullmatch(r"#[a-z][\w-]{1,30}", cat) and cat not in _GENERIC_TAGS:
        out.append(cat)
    return out
