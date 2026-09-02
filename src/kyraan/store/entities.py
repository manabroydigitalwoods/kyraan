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
    "transaction txn id bank amount total date time online address code "
    "name number no distributor customer copy".split())
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
        # the model is optional; the deterministic category is not
        # (live 2026-09-02: a failed local call left a cash memo with
        # no #receipt at all)
        log_event("entity_extract_failed", error=str(exc)[:100])
        fallback = category_from_words(f"{hint} {text[:400]}")
        return [fallback] if fallback else []
    return clean(data.get("entities") or [], text,
                 category=str(data.get("category") or ""), hint=hint)
    return out


def clean(items, text: str, category: str = "", hint: str = "",
          contained: bool = True) -> list:
    """The one gate every entity list passes — the local extractor's and
    the vision call's alike (live 2026-09-03: the vision model returned
    'Wellbeing Nutrition #health', '#Plant Based #nutrition' — tags
    glued onto entities, several categories — and photo.py stored them
    raw). Rules: an entity is accepted only if the text literally
    contains it and it is not made of generic words; inline #tags are
    split off and the FIRST valid one becomes THE category; exactly one
    category per document, deterministic fallback when none is valid."""
    low = str(text or "").lower()
    cat = str(category or "").strip().lower()
    out, tags = [], []
    for raw in items:
        item = " ".join(str(raw).split())[:60]
        # split glued tags: "Wellbeing Nutrition #health" -> entity + tag
        parts = re.split(r"(?=#)", item)
        words_only = " ".join(p for p in parts if not p.startswith("#")).strip()
        tags += [p.strip().lower() for p in parts if p.startswith("#")]
        item = words_only
        words = item.lower().split()
        if (len(item) >= 2 and (item.lower() in low or not contained)
                and item not in out
                and not all(w in _GENERIC for w in words)
                and item.lower() != cat.lstrip("#")):
            out.append(item)
        if len(out) >= _MAX:
            break
    # The category is a HUB: one name per family, or the graph splits
    # (#health beside #medical beside #medicine). The deterministic
    # family wins whenever the words match one; the model's tag only
    # names what no family covers.
    fallback = category_from_words(f"{hint} {str(text or '')[:400]}")
    if fallback:
        out.append(fallback)
        return out
    for t in [cat] + tags:
        t = t.replace(" ", "-")
        if re.fullmatch(r"#[a-z][\w-]{1,30}", t) and t not in _GENERIC_TAGS:
            out.append(t)
            break
    return out


_CATEGORY_WORDS = (
    ("#receipt", ("cash memo", "receipt", "invoice", "bill", "payment", "paid")),
    ("#medical", ("prescription", "vaccination", "vaccine", "clinic", "hospital",
                  "dose", "medicine", "medication", "lozenge", "syrup", "ointment",
                  "tablet", "throat relief", "pain relief")),
    ("#ticket", ("ticket", "boarding", "pnr", "seat")),
    ("#card", ("visiting card", "business card", "id card", "aadhaar", "pan card")),
    ("#supplement", ("supplement", "capsule", "omega", "vitamin")),
    ("#contract", ("agreement", "contract", "terms")),
)


def category_from_words(text: str) -> str:
    """Deterministic category when the model offers none: the first
    keyword family the text matches ("Cash Memo" -> #receipt)."""
    low = str(text or "").lower()
    for tag, words in _CATEGORY_WORDS:
        if any(w in low for w in words):
            return tag
    return ""
