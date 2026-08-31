# Kyraan — Master Plan
**Knowledgeable Yielding Responsive Autonomous Agent Network**

A personal multi-agent AI assistant for family life (home automation, planning, reminders) and work (Digitawoods / Portalapp), designed to evolve safely over time through memory, reflection, and guardrails — not autonomous self-modification.

---

## 1. Vision & Design Principles

- **Not a single bot — a network of narrow, specialized agents** coordinated by a central control plane.
- **"Evolves day by day" means:** growing memory + growing skills + tuned rules from feedback — never uncontrolled self-modifying weights.
- **Every autonomous action is auditable and reversible.** If Kyraan does something wrong, you can trace *why* by reading a memory file or a log — never "somewhere in the weights."
- **Conservative by default.** When uncertain, ask — don't guess. Especially for anything physical (locks, devices) or costly (money, client communication).
- **Build the slow, forgiving mode (text) first.** Voice/vision come later, once the core is trustworthy — they expose weaknesses faster and give less room to recover.

---

## 2. Full System Architecture

```
                         ┌─────────────────────────────┐
Scheduled/Event Triggers │   Proactive Trigger Layer    │
   (reminders, briefs) → │   (cron / event-based)       │
                         └──────────────┬───────────────┘
                                        ↓
User Input (chat/voice-later) ──→ ┌─────────────────────────────┐
                                   │   Control Plane / Kernel     │
                                   │  (permissions, kill switch,  │
                                   │   logging, coordination)     │
                                   └──────────────┬───────────────┘
                                        ↓
                          ┌───────────────────────────────┐
                          │  Intent Normalization Layer     │
                          │ (typo/slang handling, entity     │
                          │  disambiguation, confidence)     │
                          └──────────────┬────────────────┘
                                        ↓
                          ┌───────────────────────────────┐
                          │        Agent Router             │
                          │ (home / work / family; single    │
                          │  or multi-agent coordination)    │
                          └──────────────┬────────────────┘
                                        ↓
                          ┌───────────────────────────────┐
                          │        Model Router             │
                          │ (cheap → mid → frontier tiers,   │
                          │  fallback escalation)            │
                          └──────────────┬────────────────┘
                                        ↓
                          ┌───────────────────────────────┐
                          │     Agent(s) + Skills            │
                          │ (narrow, scoped capability        │
                          │  packages: tools + instructions)  │
                          └──────────────┬────────────────┘
                                        ↓
        ┌───────────────────────────────┼───────────────────────────────┐
        ↓                               ↓                               ↓
┌───────────────┐            ┌───────────────────┐            ┌───────────────────┐
│ Memory Layer    │           │ Extraction (async)  │            │ Curiosity Queue     │
│ MD files + RAG  │  ←write── │ conservative,        │  ──flags──→│ (batched, rate-     │
│ + Relationship   │           │ check-before-write   │            │  limited questions)│
│ Graph            │           └───────────────────┘            └───────────────────┘
└───────────────┘
        ↓
┌─────────────────────────────┐
│      Response Engine          │
│ (merges results + questions,  │
│  formats per channel, applies │
│  DND/timing gating & tone)    │
└──────────────┬───────────────┘
               ↓
         Output to you (Telegram / app / voice-later)

Parallel/ongoing:
- Reflection Loop → periodically reviews logs, tunes extraction/routing rules (versioned, reversible)
- Cost Monitor → tracks spend per tier, alerts on budget caps
- Eval Harness → tests routing/extraction/normalization against known-good scenarios after changes
```

---

## 3. Component Reference

| Component | Purpose |
|---|---|
| Control Plane / Kernel | Central coordination: permissions, logging, kill switch |
| Kill Switch | Instant freeze of all autonomous action, independent of other guardrails |
| Proactive Trigger Layer | Initiates action without user input (reminders, briefs, checks) |
| Intent Normalization | Handles typos/slang/shorthand, resolves to structured intent + confidence |
| Entity Disambiguation | Resolves ambiguous names (person vs. place) using memory first, context second |
| Agent Router | Classifies domain, handles single vs. multi-agent (cross-domain) requests |
| Model Router | Routes each call to cheap/mid/frontier model tier, with fallback escalation |
| Agents (Home / Work / Family) | Narrow, domain-scoped executors |
| Skills | Reusable capability packages (instructions + tools + permission level) per task type |
| Memory — MD files | Precise, durable facts (people, routines, preferences) |
| Memory — Vector RAG | Episodic/conversational recall |
| Memory — Relationship Graph | Entity connections (people ↔ projects ↔ tasks ↔ schedules) |
| Extraction | Async, conservative fact-saving; check-before-write; typed routing to correct store |
| Reflection Loop | Periodic self-review of logs; tunes rules/prompts (versioned, reversible) |
| Curiosity Queue | Surfaces memory gaps as batched, rate-limited questions |
| Clarifying Questions | Blocking, task-specific questions (different from curiosity) |
| Response Engine | Merges all outputs into one coherent, channel-appropriate reply; enforces DND/gating |
| Retrieval Infra | Postgres + pgvector + full-text search + triples table (single DB for durable data, pragmatic scale) |
| Session/State Store | Redis: short-term conversation memory, session state, cost counters, ephemeral queues — volatile-by-design state that shouldn't live in Postgres. Until it enters (Phase 3), these live in process memory + small JSON files |
| Web Search | Used only to enrich an *already-identified* entity — never to disambiguate |
| Per-Person Visibility Rules | Governs whose data is visible to whom within the family |
| Conflict Resolution | Rule for handling contradicting info from different family members |
| Cost Monitoring | Tracks spend per model tier; hard budget caps + alerts |
| Outage Fallback | Local fallback for critical home actions if cloud/model APIs are down |
| Onboarding/Bootstrap | Structured initial data seeding (not organic-only growth) |
| DND/Timing Rules | Governs when proactive output is appropriate to send |
| Eval Harness | Test set to verify self-tuning components are actually improving |
| Backup/Durability | Plan for memory store backup and recovery |

---

## 3a. Governance, Consent & Boundary Gaps (must decide before Phase 3)

> **RESOLVED 2026-08-27 — the decision record is [governance.md](governance.md)**
> (all eight gaps decided; Phase 3 is unblocked and must conform to it).
> The table below remains as the original statement of the gaps.

These are policy decisions, not just technical components — they shape the data model and can't be safely bolted on after multi-user rollout.

| Gap | Why it matters |
|---|---|
| **Family consent** | Wife, son, parents are being remembered/profiled (routines, schedules, possibly health-adjacent info). Need explicit agreement from each person whose data is stored — especially relevant if son is a minor. |
| **Work/personal data boundary** | Portalapp data (deploys, tickets, client info) and family data would share infrastructure. Needs an explicit boundary — check Digitawoods' data policy on routing work info through a personal AI + third-party model APIs; likely needs a fully separate scope, not just a permission tag. |
| **Third-party model data exposure** | Every request (including sensitive family/work data) goes to an external model API. Decide upfront what categories are acceptable to send externally vs. what should stay local. |
| **Voice/recording consent (future)** | Once continuous voice is added, recording family members may have jurisdiction-specific consent requirements. Flag now, address before Phase 5. |
| **Maintenance ownership** | The system needs ongoing attention (reflection tuning, skill additions, extraction review) to stay reliable. Decide realistic time budget given your CTO workload, and what "safe to leave alone" looks like if unmaintained for a while. |
| **Review-scaling plan** | Manual review of extraction/reflection changes is right for v1 but doesn't scale. Decide the point at which review shifts to sampling, and what triggers full review again (e.g., after a bad tuning pass). |
| **Direct correction/undo path** | A simple, immediate "that was wrong — undo/fix it" command for you, separate from waiting on the reflection loop to notice a pattern. |
| **Staged rollout to family** | Sequence: prove the system with yourself first, then extend to wife/parents/son — rather than rolling out to everyone on an unproven system simultaneously. |

---

## 3b. Tooling Layer & Loop Engineering (inside "Agents + Skills")

These sit *inside* the Agent/Skill execution box in the architecture diagram — tooling defines what an agent *can* do; loop engineering defines *how* it acts when a task takes more than one step.

**Tooling Layer**
- **Tool registry** — central list of all tools (Home Assistant, calendar, Portalapp APIs, memory store, web search), each with a clear schema: name, parameters, return type, permission level. Skills/agents reference tools from here rather than each defining their own.
- **Narrow, single-purpose tools** — e.g. `get_calendar_events`, `create_reminder`, not broad catch-alls. Easier for the model to use correctly and easier to permission precisely.
- **MCP as the standard** for custom tools (Home Assistant bridge, Portalapp API) — standardizes discovery, allows swapping/adding tools without rewriting agent code.
- **Failure handling per tool** — defined retry/fallback/surface-to-user behavior for every tool call (concrete implementation of the failure-recovery gap already listed).
- **Tool testing in isolation** — each tool independently testable, feeding into the eval harness.

**Loop Engineering** (multi-step task execution)
- **Loop pattern** — observe → think → act → observe (ReAct-style) for open-ended multi-step tasks, or plan-then-execute for tasks with a known sequence. Single-tool-call tasks skip looping entirely.
- **Hard iteration limits** — every loop has a max-steps cap to prevent runaway execution (directly tied to cost monitoring).
- **Loop-detection** — if the same tool/input repeats or state isn't changing, break out and ask rather than spin silently.
- **Cost-aware looping** — cheap-tier model for straightforward steps, escalate to frontier tier only when a loop is stuck or genuinely complex.
- **Human checkpoint mid-loop** — for guardrail-gated actions (locks, spending, client emails), pause and confirm before the action step, not just at final output.

---

## 3c. Connector & Capability Layer (decided 2026-08-28)

Verdicts from the full connector-architecture proposal review, so the good
parts land and the rejected parts stay rejected with their reasons on
record. Ground rule carried over from the proposal's own best idea: the
agent sees *capabilities* (`email.*`, `contacts.*`), never provider-branded
tools — a rule the proposal itself broke for WhatsApp/Slack/GitHub.

**Adopt now — BUILT 2026-08-31 (taint names, contracts, search_facts, error names)**
- **Trust/taint classes** — name the taxonomy (`EMAIL_UNTRUSTED`,
  `WEB_UNTRUSTED`, `CONTACT_DATA`, …) over the mechanisms already enforced
  ad hoc (`web_tainted` write-lockout, email cloud-exclusion, review-listing
  placeholders). One checked place instead of per-adapter re-derivation.
- **Capability-contract metadata** — declare `effect` / `risk` /
  `requires_confirmation` / `verification` per tool, next to the existing
  registry entries. The behavior already exists (`_describe_call`, confirm
  gates, `_READ_ONLY_TOOLS`); this makes it auditable data.
- **`memory.search_facts`** — a real retrieval gap; episodes are searchable,
  reviewed facts only reachable via context assembly or relations.
- **Normalized error names** (`AUTH_REQUIRED`, `RATE_LIMITED`, …) — a thin
  mapping over `ToolError`/`TransientToolError`, not a new system.
- **`system.status` (READ ONLY) — adopted 2026-08-28.** Memory pressure
  (wired/free/compressed, swap), the four local Docker containers
  (Postgres/Redis/Home Assistant/SearXNG), and currently-loaded Ollama
  models — one tool, one call, no arguments (zero injection surface).
  Ships with `system.status` only; `system.restart_*`/stop/update/delete
  tools are a SEPARATE decision, gated below, not a natural next step of
  this one.

**Adopt next (the one substantial win)**
- **Google Contacts → IdentityResolver → persons.** Known OAuth model, feeds
  the people-graph directly, no governance conflict. Preconditions: a new
  row in governance §0's data-destinations table first (contact names/
  numbers entering cloud prompts is a policy event per §3), and
  `contacts.sync` runs as a nightly job — never as an agent-callable tool.
  Normalized contact schema is provider-neutral so a second provider
  (iCloud) slots in *if* it ever clears its own gate.

**Gated — each needs its own governance round (a `Decide:` line) before design**
- WhatsApp (unofficial automation risks banning the owner's personal
  number — product decision, not an estimate line), Slack + GitHub (work
  data; governance §2's three conditions remain unmet), multi-account /
  `google_work` (same §2), `web.open`/fetch (SSRF + prompt-injection
  surface; the snippets-only line is deliberate — loosening it requires
  extending the taint write-lockout to fetched content), device
  presence/location (family consent, §1/§8).

**Rejected, with reasons on record**
- Six-kernel runtime, ConnectorRegistry-as-infrastructure, Source
  Planner / `context.search` — re-creates the dispatch layer the agent
  loop measurably beat; the loop IS the planner (phase3_architecture §1).
- Dynamic per-turn tool exposure — invalidates the byte-stable prompt
  prefix that bills ~47% of input at the cached rate; trades a measured
  saving for a speculative one, to solve a menu-size problem only the
  proposal's own scope expansion creates.
- `system.restart_container` / `system.*` write/update/delete — Kyraan
  restarting its own Postgres is a self-outage vector (governance §5).
  **Owner's call (2026-08-28): read-only now; writes reopen once Kyraan
  has proven trustworthy.** Made concrete rather than left as a feeling,
  mirroring governance §8's clean-soak-day bar — the graduation criteria
  are ALL of:
  1. 30 consecutive soak days with zero unconfirmed writes anywhere in
     the system (the existing stage-1 exit bar, §8) run again from
     whatever day `system.status` goes live;
  2. the eval gate green throughout — a single regression resets the
     clock, the same rule §8 already applies to privacy-boundary bugs;
  3. `system.status` itself has been actually relied on to answer a real
     question at least a handful of times, not just shipped and ignored.
  Even once cleared, scope narrow: reversible/idempotent operations
  (restart a container) before anything destructive, each still behind
  the standard confirm gate — this bar authorizes DESIGNING the write
  tools, not shipping them unconfirmed.
- Directory restructure into `integrations/` — churn across 660+ tests
  for zero behavior change; revisit only if a second provider for the
  same capability actually lands.
- iCloud mail/calendar/contacts as a near-term target — app-specific
  passwords have no refresh flow and break silently; highest-maintenance
  integration in the proposal against a 2h/week budget (§5). Reopen only
  with a concrete need Google doesn't cover.

*Rationale: about a quarter of the proposal described things already
built and property-tested (dynamic capability brief, outcome readback,
commit-state honesty, per-tool failure policy, read-only task scopes).
The honest delta is contacts + metadata + taint names — so that is the
plan.*

---

## 3d. Competitive Gap Analysis (2026-08-31)

Benchmarked against what ships this month: Hermes Agent (Nous Research,
self-hosted, 15+ channel gateway, self-improving skills), ChatGPT Work
(agent mode, connector directory, agentic scheduled tasks), and Claude
Cowork (950+ MCP connectors, multi-hour projects, cloud-run schedules).

**Where Kyraan is ahead — don't chase ghosts**
- Owner-reviewed memory (all three competitors auto-save; our review
  queue + poisoning protection is stronger governance).
- Real multi-person identity: registry, faces, stages, capability
  grants. None of them do household multi-user with access control.
- Privacy floor: email bodies, biometrics, voice audio never leave the
  machine. Only Hermes shares the self-hosted spirit.
- Delivery truth + the eval gate. No competitor exposes reliability
  discipline at any level.

**The gaps, ranked by felt impact**
1. **Long-horizon work.** ChatGPT Work and Cowork run multi-hour,
   multi-step projects to a finished deliverable; our agent tasks run
   one read-only instruction. This is the goal/task-continuity build —
   already owed a 20-minute design conversation (§7).
2. **Acting in the world (page reading first).** Cowork browses and
   fills forms; we can't open a page ("open first news", live
   2026-08-30). `web.open` stays GATED per §3c (SSRF + injection;
   requires extending the taint write-lockout to fetched content) —
   the gap entry here is a reason to run that governance round, not a
   bypass of it. Email sending and acting rules remain owner-held by
   decision: deliberate, not gaps.
3. **Connector breadth via MCP-as-client.** Claude lists 950+ MCP
   servers; we have 11 hand-built adapters. We will never match breadth
   by hand — one MCP client adapter mounting external servers behind
   our own permission registry (capability names, confirm gates, taint
   classes per §3c) turns their ecosystem into ours. New in this round;
   needs its own governance row for each mounted server's data
   destination.
4. **Availability.** Cowork schedules run in the cloud with no device
   online; Kyraan lives on a MacBook that sleeps (swallowed
   vaccination reminder, 2026-08-30 — misfire fix makes us
   late-but-honest, not awake). Near: pmset/caffeinate wake scheduling
   around due jobs. Later: an always-on box; the Docker stack ports
   cleanly.
5. **Channels.** Hermes speaks 15+ (WhatsApp, Signal, Slack, SMS,
   iMessage…); Kyraan speaks Telegram only — if Telegram is down we
   are mute. A second channel (CLI first: zero governance surface)
   proves the `channels/` abstraction. WhatsApp remains gated (§3c:
   ban risk on the owner's personal number).
6. **Self-improvement loop.** Hermes' agent-curated skills grow with
   use. Our analog is thinner: self-review → corrections → eval suite,
   by hand, weekly. Modest version: promote repeated confirmed
   corrections into persona/prompt rules through the same
   owner-review-queue pattern facts already use. Never self-modifying
   weights (§1).

**Build order (proposed 2026-08-31, owner not yet committed)**
1. Web page reading — small, closes a live user-visible failure
   (governance round first, per §3c).
2. Goal/task continuity — the design conversation in §7.
3. MCP client adapter — one build, buys an ecosystem.
4. Wake-scheduling around due jobs — cheap insurance.
5. Second channel (CLI) — proves the channel layer.

---

## 4. Deferred (Explicitly Not v1)

- OCR / Vision input
- Continuous voice (streaming STT/TTS, interruption handling, always-listening)
- Fully autonomous "human-like" physical action
- Fine-tuning / continuous model learning

*Rationale: these raise the stakes of every existing gap (guardrails, disambiguation, memory accuracy) and should only be added once the text-based core is proven reliable.*

---

## 5. Phase-Wise Build Plan

### **Phase 0 — Foundations (before writing agent logic)**
- Finalize tech stack (language, hosting, DB — likely Postgres + pgvector)
- Set up Control Plane skeleton with **kill switch** built in from day one
- Set up logging/observability (every decision, tool call, routing choice)
- Define permission/guardrail config format (even a YAML file)
- Bootstrap/onboarding: seed initial family + Portalapp facts manually

### **Phase 1 — Core Brain (v1, minimum viable)**
- Single orchestrator + Control Plane, one channel (Telegram bot)
- Model Router: 2 tiers (cheap + frontier) to start
- Memory: **MD files only** (skip RAG + graph for now)
- Extraction: conservative; **manually review writes** for the first few weeks
- Intent normalization: typo/slang handling via cheap-model classification
- DND/timing rules + basic proactive triggers (reminders only)
- Goal: reliable reminders, planning, simple Q&A — trustworthy before expanding

### **Phase 2 — Tool Integrations**
- Build the tool registry (schema, permission level per tool) before adding individual tools
- Calendar, email/Slack (work), Home Assistant (home automation bridge) — built as MCP servers where possible
- Portalapp-specific tools scoped to your CTO workflows
- Define failure handling per tool (retry/fallback/surface-to-user)
- Basic loop engineering: iteration caps, loop-detection, human checkpoint before guardrail-gated actions
- Cost monitoring (basic daily spend log to start)
- Outage fallback for core home actions (local control path)

### **Phase 3 — Multi-Agent Specialization**

> **AMENDED 2026-08-27** — the accepted design is
> [design/phase3_architecture.md](design/phase3_architecture.md): **one
> loop with scoped contexts**, not multiple agents behind a router.
> Connector/capability additions follow §3c's verdicts (2026-08-28):
> taint names + capability metadata + `memory.search_facts` now, Google
> Contacts next, everything else gated or rejected there.
> Phase 2's live evidence (the classifier/dispatch architecture losing
> to the single tool loop) superseded this section's agent-split plan;
> the split's real goals — capability boundaries, data boundaries — ship
> as per-person tool scopes and a visibility layer. The Work agent is
> deferred by governance §2. The items below otherwise stand.

- **Prerequisite:** resolve governance gaps first — family consent, work/personal data boundary, third-party data exposure policy, staged rollout sequence (see Section 3a) — **RESOLVED, see governance.md**
- Split into Home / Work / Family agents with defined ownership boundaries
- Build Agent Router (classifier + multi-agent coordination for cross-domain requests)
- Introduce Skills as reusable, permissioned capability packages
- Stand up the datastore layer: **Postgres + pgvector** (durable memory —
  facts, RAG embeddings, full-text, triples) and **Redis** (short-term
  conversation memory, session state, cost counters, ephemeral queues).
  Postgres owns everything durable; Redis owns only state that is allowed
  to vanish. MD files remain the human-reviewable source of truth for
  facts, synced into Postgres for retrieval
- Add Vector RAG (episodic memory) and Relationship Graph (entity connections)
- Multi-user identity: per-person visibility rules, conflict resolution logic
- Add direct correction/undo command path
- Define review-scaling plan (full review → sampling, with re-trigger conditions)

### **Phase 4 — Autonomy, Growth & Governance**
- Reflection loop: rule/prompt tuning from logged outcomes (versioned, reversible)
- Curiosity queue: batched, rate-limited proactive questions
- Clarifying-question mechanism refined (blocking, task-scoped)
- Eval harness: test set for routing/extraction/normalization quality
- Backup/durability plan for the full memory store
- Full guardrail maturity: per-skill permission levels, confirm-before-action rules

### **Phase 5 — Future Expansion (post-core, deliberately deferred)**
- OCR/vision tool integration
- Continuous voice (streaming, interruption-aware pipeline)
- Expanded autonomous action scope, only after guardrails are battle-tested

---

## 6. Key Design Rules to Never Skip

1. **Extraction only saves what was *stated*, never inferred** — check-before-write, always.
2. **Curiosity questions are batched and rare** (1–2/day max); clarifying questions are immediate and task-scoped — never conflate the two.
3. **Web search enriches known entities — it never disambiguates *which* entity was meant.** Memory + context resolve ambiguity; search adds external facts after.
4. **Every autonomous action has a defined permission level** (auto / confirm-first), attached at the skill level, not just the agent level.
5. **Kill switch and DND rules exist from day one** — cheap to build, and everything else's safety depends on them.
6. **No component is "self-modifying" without a human-reviewable, versioned, reversible layer in between.**

---

## 7. Open Threads (updated 2026-08-31 — Phases 1–4 engineering complete)

Continuing but NOT completed, in priority order:

1. **Owner hygiene (oldest open thread, owner-side):** credential
   rotations — bot token revoke/reissue, ICS URL, Google client
   secret, HASS token — plus re-enrolling Kiaan's face from 3–4 clear
   photos.
2. **§3c "adopt now" items — BUILT 2026-08-31** (taint-class taxonomy,
   capability contracts, `memory.search_facts`, normalized error
   names). Still open from §3c: Google Contacts sync ("adopt next")
   behind its governance row.
3. **Goal/task continuity — BUILT 2026-08-31** (design conversation
   held in-chat; record: docs/design/goal_continuity.md). Goals with
   steps + findings journal, daily read-only research cycles at the
   goal person's own viewer context, progress-only pings, brief line.
   §3d gap #1 closed for v1; autonomous-write cycles remain gated.
4. **Rollout calendar (by design, not late):** 30 clean soak days →
   ~2026-09-26, plus the §6 sampling gate (200 reviewed / ≥90%
   trailing) before family stage-2.
5. **CI — deferred (owner's call, 2026-08-31):** a workflow was built
   and briefly pushed, then removed the same day at the owner's
   direction ("don't require CI now"). The local gate stands: full
   suite + eval before every deploy. Reinstate from git history
   (commit 9583df2) when wanted.
6. **Optional, outside every exit bar:** P3.8 advisor personas.

See [progress.md](progress.md) for the build ledger against this plan.
