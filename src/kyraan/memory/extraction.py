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
"tomorrow"/"next week". Each fact must be self-contained and understandable
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


async def propose_from_message(raw_text: str) -> list[str]:
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
        response = router.call(
            prompt=args["text"],
            system=_EXTRACT_FACTS_SYSTEM.format(now=local_now().isoformat()),
            tier="cheap",
        )
        try:
            data = json.loads(router.strip_code_fence(response.text))
            facts = data.get("facts") or []
        except (json.JSONDecodeError, AttributeError):
            # Extraction is best-effort by design — malformed model output
            # means "no facts this time", never an error the user sees.
            log_event("extraction_malformed", raw=response.text)
            return []

        queued = []
        for fact in facts[:_MAX_FACTS_PER_MESSAGE]:
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
