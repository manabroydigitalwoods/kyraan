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

## 7. Immediate Next Step

Define the **Phase 1 v1 scope** in concrete technical terms (stack, repo structure, first Telegram bot skeleton, MD file schema) and start building — with Phases 2–5 layered in only once Phase 1 is running reliably for a few weeks.

See [progress.md](progress.md) for what's actually been built against this plan so far.
