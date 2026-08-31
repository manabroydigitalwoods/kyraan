# Kyraan 2.0 Architecture

> **Kyraan 2.0 is a bounded, safety-governed cognitive execution
> system.** It uses engineered loops for reasoning, tool execution,
> read-after-write verification, permissions, recovery, and
> post-turn learning — with named terminations in every turn trace.
> Reflection (nightly self-review), curiosity (daily gap questions),
> consolidation (nightly memory dedup), and goal continuity (daily
> read-only research cycles) exist as governed v1 loops; their
> deepening — and any autonomy beyond research-and-propose — remains
> deliberate, gated work. (Positioning adopted from the 2026-08-31
> external review, corrected to what is actually built.)

The end-to-end turn pipeline as built — a self-hosted personal assistant on
Telegram: one frontier brain, deterministic rails on every side of it, memory
the owner governs. Snapshot of 2026-08-28 · 677 tests · eval gate 21/21 hard +
11/11 soft.

> **The doctrine the whole system enforces:** the model decides what the user
> *means* and proposes what to do. The harness decides what Kyraan may
> *believe*, *remember*, *execute*, and *claim succeeded*. Every behavior fix
> that mattered became a deterministic rail, never a prompt plea.

## The turn pipeline

```
Telegram input
  │
  ▼
1. CHANNEL & INGRESS                                        [deterministic]
  │  owner/stage gate · burst window (fragments join) · restart history
  │  seeding · voice → local Whisper · photo → vision turn · PDF/docx/txt/csv
  │  → local extraction + original stored + sha256 dedup · location pins
  ▼
2. BEFORE-LOOP RAILS                                        [deterministic]
  │  confirmation consumption (nonce, 300s TTL, Redis-persisted) · orphaned-
  │  ask honesty · bare acks → 👍 · time-fragment patience (question-aware) ·
  │  command turns (undo / review memory / forget doc·face / health report /
  │  consolidate) · "its me" self-claim enroll · correction capture
  ▼
3. CONTEXT ASSEMBLY                                         [deterministic]
  │  doctrine + capability brief + stage-filtered tool menu (~44 tools;
  │  unconfigured/un-consented tools never appear) · reviewed memory block ·
  │  pending facts (reviewer-keyed) · clipped redacted history · RAG previews
  │  (episodes ≥0.35, doc chunk ≥0.28 or FTS hit) labeled "previews, never
  │  facts".  Static ≈6.1K tok (78% cache-hit); volatile ≈1.8K tok.
  ▼
4. AGENT LOOP                                             [model judgment]
  │  gpt-5.4-nano → local qwen3:8b (degraded) → honest outage message.
  │  ≤5 JSON decisions per message: reply, or call a tool and SEE the result
  │  before deciding again. No pre-loop classifier (measured, deleted); no
  │  plan-first (incremental decide-observe won).
  │  In-loop rails: deflection guard (≤2) · referent rail (sole named person)
  │  · false-success guard (≤3) · listing grounding for deletes · web taint
  │  (writes lock after web.search) · repeat/loop detector · malformed retry.
  ▼
5. ACTION KERNEL                                            [deterministic]
  │  permission registry (auto/confirm) · per-stage toolsets · confirm ask
  │  shows the ACTUAL action · yes replays the stashed call byte-identically
  │  (survives restarts) · $5/day + per-person budgets · provider cooldowns ·
  │  kill switch blocks everything.
  ▼
6. EXECUTE · VERIFY · UNDO                               [code + adapters]
  │  Prior state observed BEFORE every destroy → complete undo matrix
  │  (event, reminder incl. recurrence, task, watch rule, memory unforget
  │  with suppression lift). Home switches poll HA to convergence —
  │  converged=false renders "the device hasn't confirmed". Receipts are
  │  templated in code; the model never narrates outcomes.
  │  Adapters: calendar · gmail (metadata; bodies local-only; drafts opt-in,
  │  NO send path exists — a test greps for it) · home assistant · web
  │  (SearXNG) · weather · routes/places · documents+originals · faces
  │  (on-device) · files out (text formats, requester's chat only).
  ▼
7. AFTER-LOOP                              [model proposes · code disposes]
  │  reply → history + chat log (cloud_text redactions) · fact extraction:
  │  propose → anti-fabrication word-overlap → dedup → path normalization →
  │  sensitivity flags → OWNER'S REVIEW QUEUE (nothing durable without a
  │  promote; earned sampled mode = 24h objection windows) · turn_health
  │  verdict · in-band health alerts (owner turns) · session summary rolls ·
  │  action-log row.
  ▼
8. BACKGROUND COGNITION                                        [scheduled]
     episode catch-up every 30 min (recall ≤30 min behind live; unchanged
     chunks skipped) · watch rules every 15 min (notify-only; DND-held
     alerts re-fire unburned) · home alerts every 30 min · briefs 7:30 &
     21:30 · nightly 21:45: self-review, full episode ingest, forget
     re-sweep, graph catch-up, auto-approvals, semantic dedup scan,
     cross-person conflict scan, health report on WARN.
```

## Memory, by class

Separate stores with separate retrieval — never one vector soup. Everything
joins on the person-registry id through one deterministic name resolver
(aliases: "Maan" → owner, "Habu" → kamal).

| Store | What | Where | Governance |
|---|---|---|---|
| facts | owner-reviewed semantic memory: spheres, flags, embeddings | files (authority) + PG mirror | review queue; author + subject stamped; §4 visibility; disputes |
| episodes | past conversations, session-gap chunked | PG + pgvector | similarity floor 0.35; forget cascade suppresses |
| documents | captured photos/PDFs/files: text + original bytes | PG + `data/documents/` | sha256 dedup; registry-validated subject links; exposure gating |
| graph | typed relations with source-fact provenance | PG triples | served only while the supporting fact is active |
| persons | registry: household + access-less contacts, aliases | PG | stages gate everything; enrollment is an owner ceremony |
| faces | biometric templates | local JSON only | confirm-gated writes; never leaves the machine |
| tasks | reminders · scheduled tasks · watch rules · action log | files + PG | undo matrix complete; DND-gated delivery |
| session | working memory, confirm stash, summaries | Redis (file fallback) | restart-invisible; FLUSHALL loses no spend |

## Model roster

| Role | Model | Why pinned |
|---|---|---|
| agent loop, extraction, vision, tagging | gpt-5.4-nano (cloud, paid) | 18/18 parity with mini at ~1/6 cost; ~$1.25/day on heavy days |
| degraded loop, email bodies, summaries | qwen3:8b (local) | privacy-boundary work + outage fallback |
| tagging fallback (non-cloud content) | ministral-3:3b (local) | probe: strict-subset misses vs llama3.2:3b |
| all embeddings (384-d) | all-minilm (local) | probe-verified retrieval precision at minimal cost |
| voice transcription | whisper-large-v3-turbo (MLX, local) | multilingual EN/BN/HI; audio never leaves |
| face detect/match | YuNet + SFace (OpenCV, local) | on-device biometrics; tuned cosine bands |

Every pin came from the probe→pin→gate pattern: candidates run a real-task
probe script, the winner is pinned with the numbers in a comment, and
`scripts/eval.py` gates every change.

## Cross-cutting layers

**Privacy boundaries**
- Email bodies: fetched under explicit opt-in, summarized by the *local*
  model, never in cloud prompts or history.
- Biometrics and voice audio never leave the machine.
- `local_only` exposure is served only to local-tier prompts.
- Pending facts ride to cloud prompts only after sensitivity classification.
- Email drafts exist; a send code path does not — a test greps the sources to
  keep it that way.

**Observability**
- One `turn_id` stamps every event and full-text trace of a turn.
- `events.jsonl` audit trail · `traces.jsonl` prompts/responses ·
  `chat.jsonl` transcript (daily-rotated, 90-day archive retention).
- 38 anomaly kinds → per-turn health verdicts → rate/first-sight alerts
  (owner-only, in-band).
- Six-probe health report on demand ("health report"), CLI, or nightly-on-WARN.

**Quality gates**
- 677-test suite; timezone-pinned; every store isolated from production data.
- Golden eval: 21 hard + 11 soft live-conversation cases, gating every deploy.
- Nightly self-review + prompt-critic (proposals only, eval-gated).
- User-correction turns auto-captured as eval candidates.

**Failure honesty**
- Tier fallback ends in "both models unreachable — nothing was done."
- Unobserved prior ⇒ action honestly not undoable.
- Unconverged device ⇒ "hasn't confirmed the switch yet."
- Auth errors never retry; rate limits cool the provider down.
- Dropped confirmations announce themselves.

## Deliberately not built

- **Acting watch rules** — rules notify, never switch; autonomy over writes
  is a governance decision, not a feature flag.
- **Email sending** — held by the owner; drafting stops at Gmail's Drafts.
- **Browser automation** — highest-risk capability class, parked.
- **Plan-first agent, pre-loop classifier, saga transactions, epistemic
  taxonomies** — each considered and rejected against measured evidence; the
  incremental loop with deterministic rails won.

---
*Kyraan 2.0 · self-hosted on a Mac (launchd) · Postgres + pgvector + Redis +
Ollama in Docker · private repo, CI on push · owner: Manab Roy*
