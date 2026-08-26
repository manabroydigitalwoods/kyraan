# Kyraan Governance — §3a Decisions

Status: **ACCEPTED 2026-08-27** — every Decide: line below is resolved.
This file IS the decision record plan.md §3a requires; Phase 3's data
model must conform to it, not the other way around. Amendments are
edits + commits here, nothing less formal.

Convention: this file names people by ROLE (owner, spouse, child,
parents), never by name — it is git-tracked and the repo's PII scrub
stays intact.

---

## 0. Ground truth: what leaves the machine today

Policy has to start from facts. As of 2026-08-26, live-verified:


| Destination                               | What it receives                                                                                                                                                                                                               | When                                 |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------ |
| OpenAI (frontier tier)                    | Conversation text, owner-REVIEWED memory facts, tool results (except the exclusions below)                                                                                                                                     | Every agent-loop turn                |
| Local qwen3 (cheap tier)                  | Everything the frontier sees PLUS pending-review facts and email bodies                                                                                                                                                        | Extraction, summaries, degraded mode |
| Google (Calendar/Gmail APIs)              | Calendar events, email metadata queries, email bodies (fetch only — content is then processed locally)                                                                                                                         | Calendar/email tools                 |
| Google (Places/Routes APIs)               | Coordinates or place names of owner-initiated queries, incl. shared location pins                                                                                                                                              | places.nearby, routes.eta            |
| TomTom                                    | Same as above, on Google failure                                                                                                                                                                                               | routes.eta fallback                  |
| OpenAI (vision)                           | Photos the owner sends to the bot (analysis only; no tools on photo turns)                                                                                                                                                     | Photo messages                       |
| OSM Nominatim                             | Coordinates of shared location pins                                                                                                                                                                                            | Pin reverse-geocoding                |
| Open-Meteo                                | Coordinates/place names                                                                                                                                                                                                        | weather.get                          |
| Public search engines (via local SearXNG) | Search queries only                                                                                                                                                                                                            | web.search                           |
| **Never leaves**                          | Email body text and summaries, voice audio, FACE TEMPLATES (recognition is fully on-device; only a matched name enters a prompt), pending-review facts (cloud paths), HA device data beyond the prompt's readings, logs/traces | By construction, test-pinned         |

Retention at the destinations: prompts sent to OpenAI are stored by them
for up to ~30 days for abuse monitoring — not used for training per API
policy, though legal holds can extend retention; prompt-cache copies
live minutes-to-an-hour. Maps/weather/search providers see queries
transiently. The never-leaves row lists exactly the categories no
third-party retention policy can ever touch.


---



## 1. Family consent

**Proposed policy:**

- Kyraan may store facts about a family member only with that person's
informed agreement. "Informed" = they know facts are stored as local
files, sent to a cloud model inside prompts, reviewed by the owner,
and deletable on request.
- The **spouse**: ask directly before Phase 3 rollout; until they agree,
facts about them stay limited to what the owner states in passing
(current behavior) and are flagged for their review at rollout.
- The **child** is an infant: consent is the parents' joint decision
until he can meaningfully give his own. Store only what parents agree
to; revisit when he is old enough to ask.
- **Parents**: same direct-ask rule as the spouse, at THEIR rollout time
(which is later — see §8).
- Any family member can say "delete what you know about me" — that maps
to the existing forget flow and is honored without debate.

**Decide:** Adopted as written. Spouse conversation held 27-08-2026 — consent given; her facts unlock at stage 2 (§8).

## 2. Work / personal data boundary

**Proposed policy — the strict version:**

- **Kyraan is a personal system. Company data stays out.** No Portalapp
deploy states, tickets, client names, client communication, or
company credentials in memory, tools, or prompts — because every
frontier prompt goes to OpenAI under the OWNER's personal account,
which no company data-processing policy covers.
- What IS allowed: the owner's own work *schedule* (meetings on the
personal calendar, "leave by 5 for the client call") and career facts
about the owner ("CTO at the company") — facts about the owner's life,
not the company's data.
- If work tooling is ever wanted (Phase 3's Work agent), it enters only
after: (a) the company's explicit sign-off on routing its data through
the chosen model provider, (b) a separate provider account/agreement
covering it, (c) a hard scope separation so a family-context question
can never pull work data into its prompt. Until all three exist, the
Work agent of plan.md §5 is DEFERRED, not designed-around.

**Decide:** Strict version adopted 27-08-2026 — NO work items allowed.
The Work agent is formally deferred out of Phase 3; Phase 3's agent
taxonomy is Home/Family (+ owner-personal) only.

## 3. Third-party model & API data exposure

**Proposed policy — codifying the boundary that already exists in code:**

- **Never to any cloud endpoint:** email body text or summaries of it,
voice audio, pending (unreviewed) memory facts, anything a family
member marked private, credentials/tokens of any kind.
- **To the model provider (currently OpenAI) only:** conversation text,
owner-reviewed facts, and tool results needed to answer — accepting
that reviewed facts include sensitive personal ones ([HEALTH] etc.);
the review step IS the consent gate for that.
- **To maps/weather providers:** owner-initiated place names and pin
coordinates only — sharing a pin is the consent, and Kyraan never
requests or tracks location.
- **Provider changes are policy events:** repointing a tier or adding a
data-receiving tool backend requires updating §0's table in the same
commit. The capability brief's privacy answer must keep matching it.
- Standing preference: when a keyless/local option exists at comparable
quality, prefer it (the SearXNG/Open-Meteo/OSM pattern).

**Decide:** Adopted 27-08-2026 — no pull-backs; [HEALTH]-flagged facts
continue to the frontier tier under the review-gate consent.

## 4. Voice & recording consent (flagged for Phase 5)

**Proposed policy:** current voice notes are fine (owner sends them
deliberately; transcription is local; audio deleted after transcribe).
Any future always-listening or in-room capture is a new §3a round:
explicit consent from every household member present, jurisdiction check
for recording laws, and a visible hardware indicator. Not before
Phase 5, and this line item blocks it until then.

**Decide:** Adopted as written, 27-08-2026.

## 5. Maintenance ownership

**Proposed policy:**

- The owner (a working CTO) budgets **~2 hours/week** during active
phases: memory review, soak-log skim, the nightly critique, and small
fixes. Anything larger waits for a deliberate session.
- **"Safe when unmaintained" definition:** if untouched for a month,
Kyraan must degrade to at worst a stale-but-honest assistant — no
writes without confirmation (already true), budget hard-cap ($5/day,
already true), watchdog restart (already true), 90-day log retention
(already true). No component may REQUIRE weekly tuning to stay safe.
- The kill switch is the family-facing "off": anyone in the household
may ask the owner to engage it, no justification needed.

**Decide:** Adopted, 27-08-2026 — ~2 hours/week.

## 6. Review scaling

**Proposed policy:**

- Memory extraction review stays **100% manual** until 200 total
proposals have been reviewed AND the trailing-50 approval rate is
≥90%. Then: sample-review (every 3rd proposal auto-holds for review,
the rest auto-approve after a 24h objection window in which they show
as "awaiting" in answers, as today).
- **Full review re-triggers** on any of: a wrong auto-approved fact
discovered, a model-tier change, or an extraction-prompt change.
- The nightly prompt-critic stays proposals-only indefinitely; there is
no auto-apply milestone. Accepted edits gate on scripts/eval.py
(already the rule).

**Decide:** Adopted, 27-08-2026 — thresholds 200 reviewed / ≥90% trailing-50 / every-3rd held.

## 7. Direct correction / undo

**Proposed policy (mostly exists — naming it makes it a guarantee):**

- "Forget X" — deletes a fact (exists, confirm-gated).
- Stating a correction supersedes the old fact (exists).
- "That was wrong" in chat must always work as feedback WITHOUT the
owner needing to know which subsystem misfired; if it maps to no
deterministic path it lands in the self-review's signal digest.
- **Gap to close in Phase 3:** a single `undo` for the LAST write action
(delete the event just created, cancel the reminder just set) without
naming it. Until built, the per-type cancels are the path.

**Decide:** Adopted, 27-08-2026 — `undo` is a committed Phase 3 deliverable.

## 8. Staged rollout

**Proposed sequence (each stage gates the next):**

1. **Owner only** — today. Exit: 30 consecutive soak days with no
  privacy-boundary bug and no unconfirmed write.
2. **Spouse, read-mostly** — after her consent (§1): her own chat,
  Q&A/reminders/calendar-read only; no home control, no memory
   extraction from her messages for the first month (deliberate: prove
   usefulness before profiling).
3. **Spouse, full personal scope** — extraction on (her facts route to
  HER review, not the owner's — Phase 3's per-person visibility).
4. **Parents** — read-mostly indefinitely; reminders and Q&A are the
  value; home control stays owner+spouse.
5. **Child** — not before he can talk to it; parents decide then.

Multi-user identity, per-person visibility rules, and conflict
resolution (plan §3) are REQUIRED before stage 2 — that is the real
technical gate between Phase 2 and any family rollout.

**Decide:** Adopted, 27-08-2026. The stage-1 30-clean-day clock starts
27-08-2026 (the day the repository audit's privacy-boundary P1 was
fixed) — earliest stage-2 ≈ 26 Sep 2026, and any new privacy-boundary
bug resets the clock.

---



## Adoption

Accepted 2026-08-27 — all eight Decide: lines resolved above. Phase 3
design starts from §0's table and §8's gates as constraints; plan.md §3a
points here.