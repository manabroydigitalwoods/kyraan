"""Conservative fact extraction — the write half of the Phase 1 memory loop.

Design rule (Master Plan §6.1): only what the user *stated*, never inferred,
and never straight into live memory — every extracted fact goes through
store.propose_fact() into memory/pending_review/ for a human to promote or
reject (see scripts/review_memory.py). Runs as the `memory.propose` skill so
it's kill-switch-gated and logged like every other action.
"""
import json

from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.kernel import SkillCall
from kyraan.control_plane.logging_setup import log_event
from kyraan.memory import store
from kyraan.model_router import router

_EXTRACT_FACTS_SYSTEM = """You extract durable personal facts from one user message.
The current date/time is {now}.
Extract ONLY what the user explicitly states about themselves, their family,
their work, their routines, or their preferences — people the user
PERSONALLY knows. Never extract facts about public figures, politicians,
celebrities, or general/encyclopedic knowledge: those are not personal
memory and must return an empty list. Never infer. Never extract
from questions, requests, or commands — "who is Mira?" or "what time does
school start?" state nothing and must return an empty list (a reminder
request is not a fact either).
Never extract temporary states ("I'm tired"), one-off plans, or anything not
worth knowing months from now. State any dates absolutely, never as
"tomorrow"/"next week". A RELATIVE age or duration IS durable once anchored:
"my son Aarav is about 10 months old" must become the absolute form
computed from the current date ("- Son Aarav was born around October
2025") — never store the relative wording, and never drop the fact just
because it was stated relatively. Each fact must be self-contained and understandable
without the original message — "- Wife's name is Mira", never just "- Mira".
Most messages contain no durable facts — then return an empty list.
Respond with ONLY JSON:
{{"facts": [{{"path": "<category>/<slug>.md", "content": "- <one concise fact>"}}]}}
where <category> is one of: people, routines, work, preferences — and <slug>
is lowercase letters, digits, and underscores (e.g. people/wife.md,
work/woodsportal.md). Empty: {{"facts": []}}"""

# A single chat message stating more than a few durable facts is almost
# always the model over-extracting, not the user info-dumping.
_MAX_FACTS_PER_MESSAGE = 3


async def propose_from_message(raw_text: str, context: str = "", insist: bool = False) -> list[str]:
    """Extract stated facts from one message and queue them for human
    review. Returns the queued facts' content lines ([] when none), so the
    caller can tell the user what was noted."""
    # A question states nothing — enforced in code, not just in the prompt,
    # because the model was seen proposing a "fact" from "who is ruma?"
    # live despite the instruction. Conservative by design: skipping a rare
    # fact-inside-a-question costs little; polluting review costs trust.
    if raw_text.rstrip().endswith("?"):
        return []
    # A long paste is an article, not a personal statement — seen live: a
    # Wikipedia biography produced two junk proposals, one to a nonsense
    # path. Personal facts arrive in sentences, not essays.
    if len(raw_text) > 1200:
        log_event("extraction_skipped_long", chars=len(raw_text))
        return []

    async def handler(args: dict) -> list[str]:
        system = _EXTRACT_FACTS_SYSTEM.format(now=local_now().isoformat())
        if context:
            # Referent resolution: "His name is Deven" right after a
            # question about the user's father must become a SELF-CONTAINED
            # "- Father's name is Deven Roy" — the conversation supplies
            # the referent; facts are still extracted ONLY from the
            # current message. (Live: a terse "His name is biren roy"
            # reached the queue unable to say who Deven even was.)
            system += (
                "\n\nRecent conversation — use it ONLY to resolve referents"
                " ('his', 'her', 'that') so each fact is self-contained;"
                " extract facts solely from the CURRENT message:\n" + context
            )
        if insist:
            # The user EXPLICITLY asked to save ("save the kiaan age") —
            # a silent empty result here reads as a broken promise. The
            # save command may point at an earlier statement, so context
            # becomes legitimate fact material for this call only.
            system += (
                "\n\nIMPORTANT: the user EXPLICITLY asked to save this. If "
                "the message is a save command pointing at something said "
                "earlier ('save my son's age'), extract the durable fact "
                "from that referenced statement in the conversation above. "
                "Return empty ONLY if there is genuinely no information to "
                "save anywhere in the message or the referenced statement."
            )
        # Frontier-first for extraction quality (terse/fabricated facts
        # were the local 8B's signature); local fallback keeps memory
        # working when the cloud tier is exhausted — same pattern as the
        # structured extractions. Quota tracking warns before it runs dry.
        try:
            response = router.call(prompt=args["text"], system=system, tier="frontier", force_json=True)
        except router.ModelProviderError as exc:
            log_event("extraction_fallback_cheap", error=str(exc))
            response = router.call(prompt=args["text"], system=system, tier="cheap", force_json=True)
        try:
            data = json.loads(router.strip_code_fence(response.text))
            facts = data.get("facts") or []
        except (json.JSONDecodeError, AttributeError):
            # Extraction is best-effort by design — malformed model output
            # means "no facts this time", never an error the user sees.
            log_event("extraction_malformed", raw=response.text)
            return []

        message_words = {w.strip(".,!?'\"").lower() for w in args["text"].split() if len(w) > 3}
        # Context words are legitimate fact material too (the referent —
        # "father" — comes from the previous turn, not the message).
        message_words |= {w.strip(".,!?'\"").lower() for w in context.split() if len(w) > 3}
        known = store.known_fact_lines()
        queued = []
        for fact in facts[:_MAX_FACTS_PER_MESSAGE]:
            # Anti-fabrication: a real extraction reuses the message's own
            # words. Seen live (degraded mode): "make it 4 lines" produced
            # "Name is Anupam" and two more invented facts. A fact sharing
            # zero content words with the message is hallucination.
            fact_words = {w.strip(".,!?'\"").lower() for w in str(fact.get("content", "")).split() if len(w) > 3}
            if message_words and not (fact_words & message_words):
                log_event("extraction_fact_fabricated", fact=fact, source=args["text"])
                continue
            # Dedup: restating something already live or already pending
            # review is queue noise, not new memory (a duplicate wife-name
            # proposal was seen live).
            if store.is_known_fact(str(fact.get("content", "")), known):
                log_event("extraction_duplicate_skipped", fact=fact)
                continue
            try:
                store.propose_fact(fact["path"], fact["content"], source=args["text"])
            except (KeyError, TypeError, ValueError) as exc:
                # Bad shape or a path outside the allowed memory layout —
                # drop that fact, keep the rest.
                log_event("extraction_fact_rejected", fact=fact, error=str(exc))
                continue
            queued.append(str(fact["content"]))
        if queued:
            log_event("extraction_proposed", count=len(queued), source=args["text"])
        return queued

    return await kernel.run_skill(SkillCall("memory.propose", {"text": raw_text}), handler)
