# Kyraan 2.0 — Loop Engineering Audit (as-implemented, 2026-08-31)

Code wins over docs throughout. Every load-bearing claim carries its
file/symbol. Suite at audit time: 799 tests; eval gate 21 HARD + 11
soft cases. Classifications: IMPLEMENTED / PARTIAL / IMPLICIT /
MISSING.

---

## 1. Executive architecture — an actual request's path

```
Telegram update (text/photo/file/voice/pin)
  → channels/telegram_bot.py handlers (_on_message/_on_photo/_on_document/…)
      · media capability gate (_media_admitted), viewer identity set
        (kernel.set_viewer via _viewer_for) — deterministic, fail-closed
  → agents/orchestrator._dispatch                      [deterministic pre-loop]
      · fragment patience (burst combining), confirmation yes/no
        interception, review-memory flow, health report, forget-face/
        forget-document, consolidate — regex-matched deterministic
        branches that never consult a model
  → agents/agent_loop.run(chat_id, text, tier)         [the bounded loop]
      · frontier tier, then cheap tier on AgentUnavailable, then an
        honest outage reply — one brain, two tiers, zero dispatch rules
  → per step: model decision (JSON) → executor (loop_tools.TOOLS)
      → kernel.run_tool (permission wall, validation, timeout, retry
        policy) → adapter (builtin module or MCPStdioAdapter)
  → reply → deterministic rails + reply-contract adjudication
  → orchestrator: record_exchange, log_chat, log_trace("turn_end",
      termination=..., **turn_summary())
  → async post-turn: memory extraction → pending-review queue;
      episode logging; correction capture
```

Stages from the idealized flow that DO NOT exist as distinct
components: a separate "context resolution" stage (scattered — §10), a
separate planner (§4), a global verification stage (per-tool — §7).

## 2. The agent loop (agents/agent_loop.py)

Entry: `run()` → `_run_inner()`. `_MAX_STEPS = 5` decision calls per
message (line ~82). Loop shape, reconstructed from code:

```python
_termination.set("tier_failed:aborted_mid_loop")   # named endings
loop_tools.reset_turn_urls()                       # web.open provenance
for step in range(_MAX_STEPS):
    response = model(system + transcript)          # JSON decision
    decision = parse(response)                     # unparseable → AgentUnavailable
    if decision.action == "reply":
        reply = decision.reply
        # deterministic rails: deflection (≤2), referent-dodge (≤1),
        # false-success (≤3), listing grounding, repeat detection,
        # reply-contract adjudication (answers_request + reason enum,
        # ≤2 corrections, challenged_reasons set)
        if rail_fires: transcript += SYSTEM correction; continue
        _termination.set("replied"/"replied_after_correction"); return reply
    tool = decision.tool
    if not stage_allows(tool): transcript += refusal; continue
    if web_tainted and tool not in _READ_ONLY_TOOLS: blocked; continue
    if signature repeats >= 2: raise AgentUnavailable("stuck repeating")
    result = await executor(chat_id, args, raw_text)   # → kernel.run_tool
    if taint.source_class(tool) == WEB_UNTRUSTED: web_tainted = True
    transcript += result
raise AgentUnavailable("step_cap")                  # tier falls onward
```

| Stage | Status |
|---|---|
| Observe (tool results into transcript) | IMPLEMENTED |
| Understand | IMPLICIT (inside the one decision call) |
| Plan | IMPLICIT (step-by-step; no plan object) |
| Act | IMPLEMENTED (executor → kernel) |
| Verify | PARTIAL (read-after-write on calendar create/reschedule + home switches, `loop_tools._verified`; other writes trust returns) |
| Reflect (in-loop) | NOT IMPLEMENTED (SYSTEM corrections are repair, not reflection) |
| Repair | IMPLEMENTED (rail corrections, tier fallback, honest failure text in results) |
| Continue/Stop | IMPLEMENTED (named termination taxonomy in turn_end) |

Second bounded layer: `kernel.run_tool` keeps its own per-turn step
list — identical tool+args signature repeated → "that's a loop, not
progress"; `_MAX_TOOL_STEPS` cap independent of the model loop
(control_plane/kernel.py ~line 235).

## 3. Loop/turn state — fragmented, deliberately

No canonical LoopContext object. State lives in: the transcript string
(observations/history), `calls_seen` dict (repetition), `web_tainted`
bool, `contract_corrections` int, contextvars (`_termination`,
`_TURN_URLS`, kernel `_viewer`, logging `turn_id`), the confirmation
stash (orchestrator `_pending_actions` + Redis mirror), and
`turn_summary()` stage records. The 2026-08-28 and 08-31 external
reviews both proposed a canonical object; both adjudicated REJECTED —
the turn trace already records what it would hold, and a parallel
object is state maintained twice. Fragmentation is real; each fragment
has exactly one owner.

## 4. Decision/planning — step-by-step dynamic, one schema

No router, no planner, no intent classifier (the classifier was
DELETED in P3.7b — commit history; ~2,100-line test file removed).
Every step is one JSON decision: `{consider, action: reply|tool, tool,
args, reply, answers_request, reason}`. `answers_request:false`
requires reason ∈ {ambiguous_referent, missing_user_fact,
capability_missing} and is deterministically adjudicated (sole-person
check; capability challenged against the real tool list; invalid
reason rejected). Unexpected tool results: fed into the transcript;
the next decision reacts — plus tool results themselves carry
instructions ("empty = say so, never invent"; "re-searching will find
nothing more"). Re-planning is implicit and bounded by the step cap.

## 5. Tool execution pipeline (control_plane/kernel.py run_tool)

LLM proposes → executor (per-tool arg shaping, listing-proof checks,
confirm raises) → `kernel.run_tool`: stage wall (`stage_allows` —
config stage_toolsets ∪ per-person grants; the model menu is the
polite layer, this is the wall) → kill switch → per-turn loop/step
guards → `_validate_args` against the yaml schema → permission
(`confirm` requires `call.confirmed`) → `asyncio.wait_for(dispatch,
timeout_s)` → retry loop honoring `retries` (all writes declare
`retries: 0` — "never blind-retry a write", config/permissions.yaml) →
adapter (builtin module or `MCPStdioAdapter` with env, respawn,
single-flight lock) → audit events (`tool_call`/`tool_result` with
`error_name` normalization) → failure policy (surface /
fallback:<tool> one hop / silent-for-notify). Redaction: email bodies
and pending facts never reach cloud prompts (adapter-level, §3a);
history placeholders via `__direct_reply__`.

## 6. Permission & confirmation loop

"Move tomorrow's meeting to 4 PM": loop lists events (listing-proof —
ids must come from a CURRENT listing, `_listing_lookup`), proposes
`calendar.reschedule`; executor raises `kernel.ConfirmationRequired`;
orchestrator stashes `(call, handler, stashed_at)` per chat with a
fresh nonce; ask rendered by `_describe_call` (concrete values, the
SAME normalized times execution will use); inline keyboard carries the
nonce. On yes: nonce checked (stale/swapped buttons rejected —
tests/test_telegram_channel.py nonce-race test), TTL checked
(`_CONFIRMATION_TTL_S`), the EXACT stashed args execute (no
re-derivation → no mutation window), then read-after-write
verification (§7). Stash survives restarts via Redis with TTL as
expiry (orchestrator ~line 1101). Protections present: expiry, nonce
replay/swap, exact-args replay, one-pending-per-chat. MISSING: a
cross-chat/second-channel story (single-channel today, moot).

## 7. Verification — MODERATE, recently upgraded

`loop_tools._verified` (2026-08-31): calendar.create_event and
calendar.reschedule re-READ via get_event and compare start
(offset-aware); home.turn_on/off re-read entity state. Fail-soft: a
mismatch attaches "tell the user what actually stands, never claim
success"; an unreadable verify attaches "wrote OK but could not
re-read to confirm". Contracts expose `verification` as data
(registry.contracts()). False-success protection on the REPLY side:
the false-success rail (claims of done-ness without a write this turn)
and listing grounding. NOT covered: calendar.delete/update-title,
email mark/archive/drafts, faces writes, persons writes,
reminder/task/goal store writes (local file writes — lower risk).
Rating: MODERATE (was WEAK before 08-31; STRONG requires covering the
remaining write families).

## 8. Retry/repair map

- Invalid args → `_validate_args` ToolFailed → result text instructs
  the model; next step repairs (bounded by step cap).
- Unknown tool → AgentUnavailable("unknown_action") → next tier.
- Permission denied (stage) → SYSTEM line, loop continues.
- Timeout on read → retry per budget; on WRITE → no retry, honest
  "MAY still have gone through; check the actual state" (kernel).
- API error → error_name-labeled; failure policy (surface/fallback).
- Tool success but wrong → verification (§7) where covered.
- Empty result → per-tool terminal instructions (search_facts,
  documents.search, relations…); generic tools rely on the model.
- Same action repeats → §9. Stuck model → tier fallback → outage.

## 9. Repetition/infinite-loop protection

Exact tool+args signature, two layers: loop (`calls_seen`, warn at 2nd,
kill tier at 3rd) and kernel (per-turn signature list + hard
`_MAX_TOOL_STEPS`). `search_mail(A)×3 empty`: warn after 2nd, tier
killed on 3rd, cheap tier retries once, then honest outage — observed
live 2026-08-30 ("anything saved about the car") which motivated the
terminal-empty-result pattern. MISSING: semantic repetition (varied
args, same intent) and generic no-progress detection; the step cap is
the backstop.

## 10. Context resolution — real, scattered, no global resolver

- People: `store/persons.name_map/resolve` (registry + aliases) — THE
  name→person join, used by documents, faces, triples, extraction.
- Speaker: registry-derived SPEAKER header, fail-closed
  (`_identity_block`; `kernel.effective_reviewer`).
- Referring expressions: `_sole_recent_person` (contract
  adjudication), referent-dodge rail; "my wife/son" resolve via
  reviewed facts + relations graph at answer time, not a resolver.
- Listings: "that meeting/the first one" via `_listing_lookup` proof.
- Web: turn-URL provenance set. Weakness (from code): pronouns across
  turns rely on transcript + model judgment; no coreference module.

## 11–12. Memory architecture & learning loop

Path: message → async extraction (memory/extraction.py, cheap tier,
JSON facts with term/importance/era/sphere/flags; category aliases;
identity-claim filter; health/safety/milestone flags force term=long;
near-dup pending supersession) → PENDING REVIEW (files; owner
approves in-chat; §6 sampling gate configured at 200/90%) → live
memory (MD files = authority + PG mirror, embeddings, FTS) →
retrieval (`memory/engine._pg_candidates`: §4 visibility clause,
safety-flag bypass, hybrid rank; `build_context` budgeted) →
`memory.search_facts` direct search. Distinctions that exist:
history (orchestrator), episodic (store/episodes + recall), semantic
facts, triples graph, documents+moments, biometrics. Provenance:
source_statement + author stamps; supersession; forget purges pending
restatements (resurrection channel closed, P3.7a). Learning loop:
Extract IMPLEMENTED · Validate IMPLEMENTED (human gate) · Save
IMPLEMENTED · Retrieve IMPLEMENTED · Affect reasoning IMPLEMENTED ·
Correct/update IMPLEMENTED (supersession + pending replacement) ·
Bad-model-output pollution: blocked by the review gate (the Ruma
incident proved the gate matters; identity claims additionally
excluded structurally). Auto-approval only after the §6 trust bar.

## 13. Reflection — TRACE-BASED (v1)

`triggers/self_review.py` nightly: reviews the day's exchanges,
reports what looked wrong to the owner; correction capture
(`user_correction_candidate` events) feeds the eval suite (manual
weekly promotion). No in-loop "why did this fail" reasoning; no
automatic behavior change from reflection (deliberate — changes ride
the owner-reviewed pattern). Classification: TRACE-BASED REFLECTION,
not a full loop.

## 14. Curiosity — IMPLEMENTED (modest)

`triggers/curiosity.py`: deterministic gap candidates, 1/day in the
morning brief, 14-day re-ask spacing, state file. No uncertainty
scoring or proactive research. Working code, narrow scope.

## 15. Dream/consolidation — IMPLEMENTED (proposal-only writes)

Nightly memory-dedup scan proposes consolidation groups; the OWNER
applies ("consolidate memory" flow). It cannot mutate canonical
memory itself — same human gate as extraction. No clustering/
preference inference beyond duplicates.

## 16. Background jobs (channels/telegram_bot.py wiring)

morning/evening briefs (daily, 4h misfire grace), nightly self-review
+ health check + backups + document orphan sweep + memory dedup,
home_alerts (30 min), event_rules tick (15 min, edge-triggered),
episode catch-up (30 min), wake planner (15 min, pmset), reminders
(date jobs, misfire_grace=None), agent tasks (idem + redelivery
machine), goal cycles (per-cadence). All DND-gated, delivery-truthful
(sent only when landed), sleep-proof (fire late never never).

## 17. Goals — v1 continuity, not multi-hour autonomy

`triggers/goals.py` (2026-08-31): persisted Goal {steps, journal,
status, cadence, budget caps 3 active / ≤2 cycles/day}; conversational
updates; daily READ-ONLY research cycle running AS the goal's person
(viewer context swap in `_wire_goals.run_fn`); progress-only pings;
unreported-carry on failed delivery. A multi-hour autonomous
executor does NOT exist: cycles are single read-only agent runs;
writes only ever happen live behind the owner's confirm. That is the
governed design, not an accident.

## 18–19. Proactive & Home Assistant loops

Triggers that start Kyraan without a message: every job in §16.
Home Assistant: read/write via entity allowlist only
(tools/home_assistant.py; config read_entities/write_entities).
observe→reason→act→re-observe EXISTS in pieces: watch rules observe
and notify (never act, by doctrine); switch writes re-observe via
read-after-write. A closed sensor→action loop is deliberately
absent (governance: acting rules are a gated decision).

## 20. Agents/skills/tools/adapters — one runtime

"Agents" are NOT separate processes. One orchestrated runtime; the
loop is the only brain; TOOLS (loop_tools) are capability entries
(menu teaching + executor); registry ToolSpecs declare
permission/effects/failure; adapters are builtin modules or MCP
stdio children; kernel is the wall. Delegation = the loop choosing a
tool, nothing more.

## 21. Model roles (model_router/router.py, config)

frontier gpt-5.4-nano (loop decisions, vision, extraction on cloudy
turns), cheap qwen3:8b local (degraded loop, extraction, summaries),
all-minilm local embeddings, local Whisper voice. JSON forced where
parsed; fail-fast on credit exhaustion + auth; per-turn cost ledger.

## 22. Authority table (abridged)

Deterministic: permissions, confirmation, staleness, taint, listing
proof, provenance, repetition, budgets, identity, visibility, undo,
adjudication of contract claims, verification comparisons.
LLM: reply wording, tool choice within the wall, extraction
CANDIDATES, vision. Hybrid: referent resolution, empty-result
handling. Where the LLM has the most residual authority: composing
replies from tainted content (relaying, not acting) and choosing
among allowed read tools.

## 23. Safety rails inventory

Step caps (loop 5 / kernel tool steps), write-retry zero, kill switch
(kernel checks + loop entry), stage wall + grants, media capabilities,
biometrics owner-turn-only, web/page taint write-lockout +
provenance, pending-facts cloud placeholder, email bodies local-only,
listing grounding, false-success/deflection/referent rails, reply
contract, confirmation nonce+TTL, DND + daily budget caps, delivery
truth, undo matrix (completeness-tested), audit logs isolated from
tests, .env/secrets never in prompts, SSRF guard, frozen non-owner
surface test.

## 24. Idempotency & transactions

Writes: retries 0; write timeout → "outcome unknown, MAY have
landed; check state before retrying" (kernel) — at-most-once from
Kyraan's side, honest ambiguity surfaced. Reminders: claim lease +
idempotent send + stale-lease takeover with "may be a repeat" label —
at-least-once WITH honesty. Agent tasks: pending_result stash =
resend-not-rerun. Documents: byte-hash dedup. MISSING: idempotency
keys on external APIs (Google supports none for these ops), and no
multi-step transactions/sagas (rejected on record — external review
round 2 — single-write actions make them unnecessary today).

## 25. Observability

Per turn: turn_id across events/traces; tool_call/tool_result with
args, attempt, duration, error_name; model_io traces with prompts,
tokens, cost, cache hits; rail events; termination reason;
turn_summary stage timings; chat log with cloud_text redaction
distinction. An engineer CAN reconstruct why — demonstrated
repeatedly this week (car-search spiral, misfire deaths, cache
poisoning) from telemetry alone. Gap: no single "show me turn X"
tool; it's grep.

## 26. Tests (799)

Loop: test_agent_loop*.py (rails, contract, taint, step cap,
repeats). Confirmation/staleness/nonce: test_telegram_channel.py.
Verification: test_write_verification.py. Undo: test_undo_map.py.
Registry/guards: test_tool_registry.py (incl. write-timeout
never-retried, MCP transport). MCP: test_mcp_client.py. Injection/
taint: test_capability_contracts.py + loop tests. Memory:
test_extraction.py, test_memory*, test_visibility.py, test_rag.py.
Referents/identity: test_faces_enroll.py (frozen surface),
orchestrator tests. Scheduler edge cases: test_scheduler*,
test_event_rules.py, test_goals.py, test_wake.py. Golden suite:
scripts/eval.py — 21 HARD gates run on the live model, the deploy
gate.

## 27. Failure scenarios — what happens TODAY

A. Same tool forever → warn at 2, tier kill at 3, cheap tier, honest
outage. B. Success-but-nothing-changed → caught for
calendar-create/reschedule + switches (verified:false, honesty
ordered); other writes: trusted (gap). C. Stale confirm → TTL expiry
+ nonce; expired ask politely re-asks. D. Timeout-after-landing →
write not retried; reply says it MAY have landed; reminders label
"may be a repeat". E. Contradictory facts → author-stamped, both
flagged, subject-owner resolves; supersession never crosses
reviewers. F. Web content says ignore instructions → write-lockout
for the turn (deterministic); the reply may still QUOTE the text
(model-judgment residual). G. Dream infers wrong fact → impossible
to auto-commit; proposals need the owner's yes. H. User changes mind
mid-operation → "no" clears the stash; new message supersedes; undo
covers the committed case.

## 28. Actual cognitive lifecycle

INPUT → identity/capability gates → deterministic pre-loop →
context assembly (memory block, episodes, history) → bounded
decision loop (act/observe/repair) → rails + contract → verified
writes (partial) → reply → audit → async extraction → owner review
→ memory. Ideal-loop coloring: Observe 🟢 Understand 🟢 Remember 🟢
Reason 🟢 Plan 🟡 (implicit) Authorize 🟢 Act 🟢 Verify 🟡 Reflect 🟠
(nightly, not in-loop) Learn 🟡 (human-gated by design) Repeat 🟠
(goal cycles v1).

## 29. Scorecard (honest)

```
Bounded execution      9   Planning               6 (implicit; fine at 5 steps)
Tool orchestration     8   Permissions            9
Confirmation safety    9   Outcome verification   6 (3 write families covered)
Retry/repair           7   Loop detection         7 (exact-signature only)
Progress detection     5   Context resolution     6 (scattered, no coreference)
Memory feedback        8   Memory arbitration     7
Reflection             5   Curiosity              4
Dream/consolidation    4   Persistent goals       6 (continuity, not execution)
Event-driven autonomy  8   Idempotency            7 (honest at-most/least-once)
Observability          8   Testing                9
Overall loop engineering: 7.5
Cognitive-loop completeness: 6
```

## 30. Prioritized gaps

**P0** — (1) Verification coverage: email modify, calendar delete,
faces/persons writes lack read-after-write (loop_tools; extend
`_verified`). (2) Injected-text relay: taint blocks ACTIONS but the
reply can still repeat instructions/URLs from pages verbatim —
consider a deterministic no-URLs-from-fetched-pages reply rail.
**P1** — semantic no-progress detection (distinct-args, same-intent
empty reads burning steps); a coreference layer for cross-turn
pronouns; per-turn "show turn" observability command.
**P2** — goal cycles that maintain steps themselves (model updates
checklist from findings, still read-only); reflection promoting
confirmed corrections into persona rules via the review queue.
**P3** — prompt cache tending; degraded-tier latency.

---

### Final deliverable

1. **WHAT KYRAAN ACTUALLY IS TODAY** — a bounded, safety-governed
   cognitive execution system: one orchestrated runtime, one
   decision loop on two model tiers, a deterministic kernel wall,
   human-gated memory, delivery-truthful proactive jobs, v1 loops
   for reflection/curiosity/consolidation/goals.
2. **ACTUAL REQUEST EXECUTION LOOP** — §1/§2 above; step-by-step
   dynamic decisions, no planner, no classifier, harness-owned
   boundaries.
3. **ALREADY STRONG** — bounds, permissions, confirmation,
   taint/provenance, delivery truth, memory governance, testing,
   observability-by-grep.
4. **PARTIAL** — verification coverage, progress detection, context
   resolution, reflection depth, goal execution.
5. **DOES NOT EXIST** — in-loop reflection, semantic loop detection,
   coreference resolver, autonomous multi-hour execution, plan
   objects, transactions (rejected), self-modifying behavior
   (rejected by principle).
6. **TOP 10 RISKS** — uncovered write families; injected-text relay;
   TTL-cache class bugs (one found+fixed 08-31 — audit for
   siblings); single channel = single point of muteness; MacBook
   host (mitigated, not solved); model-judgment empty-result
   handling on generic tools; manual eval-promotion cadence;
   owner-side credential rotations still pending; grep-only turn
   inspection; MCP mounts (future) widening the taint surface.
7. **TOP 10 NEXT** — extend `_verified` to remaining writes; reply
   rail for fetched-page URLs; semantic no-progress; coreference
   layer; goal-cycle step maintenance; correction→persona promotion
   loop; "show turn" command; second channel (CLI); sudoers wake
   rule (owner); credential rotations (owner).
8. **LOOP ENGINEERING SCORE: 7.5/10**
9. **COGNITIVE LOOP COMPLETENESS: 6/10**
10. **RECOMMENDED NEXT MILESTONE** — *Verification completeness*:
    every confirm-gated write either read-after-write verified or
    explicitly declared unverifiable in its contract, with the
    honest wording rails already built. Smallest step that moves
    the weakest P0 number.
