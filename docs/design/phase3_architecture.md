# Phase 3 Architecture — Datastore, Recall, Multi-User

Status: DRAFT v2 (2026-08-27, revised same day after a 16-finding design
audit — identity/visibility/exposure/deletion models corrected, durable
stores planned, undo made state-aware, exit gates split). Conforms to
docs/governance.md (ACCEPTED 2026-08-27) — every design below cites the
governance section that constrains it. Prerequisite per plan.md §5:
governance accepted ✅.

---

## 1. Position: one brain, specialized contexts — not multiple agents

plan.md §5 ("Phase 3 — Multi-Agent Specialization") was written before
Phase 2's decisive result: the classifier architecture failed on every
question no rule anticipated, and the single agent loop (one model, one
tool menu, chained reads) replaced it as the primary brain. Splitting
that one brain back into Home/Work/Family agents with a router would
re-create the exact dispatch layer that lost.

**Phase 3 keeps ONE loop.** "Specialization" becomes three cheaper, more
testable mechanisms:

1. **Tool scopes** — a per-conversation allowlist filtered at
   `_tools_block()` time AND enforced in the executor (the same
   two-layer pattern as `read_only`). A spouse-stage-2 chat (§8)
   simply has no home-control or memory-write tools in its menu or
   its kernel permissions.
2. **Context assembly** — `memory_context()` grows a `viewer` parameter:
   what enters the prompt is the intersection of relevance (as today)
   and the viewer's visibility (§4 below). The router's real job —
   "whose data may this request see" — is a data-layer WHERE clause,
   not an agent boundary.
3. **The Work agent stays DEFERRED** (governance §2, strict version
   accepted): no company data enters the system, so there is nothing
   for a Work agent to do. Revisit only after the three §2 conditions
   exist.

Consequence: the legacy classifier path (orchestrator's frozen
fallback) is retired in Phase 3 once the cheap-tier loop passes the
same eval gate the classifier passes today — one brain, two tiers,
zero dispatch rules.

## 2. Datastore layer

### 2.1 Postgres + pgvector (durable)

Single database `kyraan`, single writer (the bot process). Schema v1:

```sql
-- People and identity (multi-user starts here, §4)
CREATE TABLE person (
  id          text PRIMARY KEY,          -- 'owner', 'spouse', ...
  chat_id     bigint UNIQUE,             -- their Telegram chat, once enrolled
  stage       text NOT NULL DEFAULT 'none',  -- governance §8: none|read_mostly|full
  consented_at date                       -- governance §1
);

-- Facts: the index.json entries, typed and owned
CREATE TABLE fact (
  id           uuid PRIMARY KEY,          -- uuid5(KYRAAN_NS, legacy_id):
                                          -- deterministic + idempotent, so
                                          -- resync never duplicates (audit)
  legacy_id    text UNIQUE,               -- the index.json short id, kept
                                          -- for joins and provenance
  subject      text NOT NULL REFERENCES person(id),  -- who it is ABOUT
  subject_reviewed boolean NOT NULL DEFAULT false,
    -- MIGRATION REALITY (audit P1): existing facts include family members;
    -- blanket subject='owner' would mis-assign visibility and review
    -- ownership. The importer derives subject from the memory tree's
    -- people/<name> paths where unambiguous; everything else lands
    -- subject='owner', subject_reviewed=false — and §4's HARD GATE: no
    -- non-owner viewer can be enabled while any active fact has
    -- subject_reviewed=false. Conservative default, explicit debt, gated.
  owner        text NOT NULL REFERENCES person(id),  -- whose review approved it
  content      text NOT NULL,
  kind         text NOT NULL,             -- mirror of memory engine kinds
  flags        text[] NOT NULL DEFAULT '{}',  -- health/safety/fun/sensitive/...
  era          text, sphere  text,
  visibility   text NOT NULL DEFAULT 'owner', -- §4: owner|shared|subject_only
  exposure     text NOT NULL DEFAULT 'cloud_ok',
    -- cloud_ok | local_only (audit P1): visibility says WHO may see a
    -- fact; exposure says WHICH TIER may carry it. Context assembly
    -- filters by the calling tier — local_only facts never enter a cloud
    -- prompt (generalizes today's pending-facts rule). Governance §3
    -- accepted no pull-backs TODAY, so every migrated fact starts
    -- cloud_ok; the knob exists so a future pull-back (e.g. [HEALTH] →
    -- local_only) is one UPDATE, not a schema change.
  active       boolean NOT NULL DEFAULT true,
  superseded_by uuid REFERENCES fact(id),
  created_at   timestamptz NOT NULL,
  source_msg   text                        -- provenance for undo/audit
);
CREATE INDEX fact_fts ON fact USING gin (to_tsvector('english', content));

-- Episodic recall: conversation chunks, embedded.
-- DDL ships in migration 002, AFTER the embedder probe pins the vector
-- dimension (audit P2: installing vector(1024) before choosing a 768-d
-- model forces a real migration with rows at stake). Column model:
--   id uuid PK; chat_id bigint; day date;
--   participants text[]         -- person ids present in the conversation
--   visibility  text            -- derived: the chat's person + owner (§4)
--   exposure    text            -- cloud_ok|local_only, as on fact
--   flags       text[]          -- sensitivity tags, applied by a local
--                               -- cheap-tier pass at write time so the
--                               -- discretion rules (§3) are ENFORCEABLE
--                               -- on episodes, not just facts (audit P1)
--   fact_refs   uuid[]          -- facts this episode evidences, when known
--   suppressed_by uuid[]        -- forget-cascade marks (see §3)
--   text        text            -- cloud_text ONLY (privacy twin)
--   embedding   vector(N)       -- N pinned by P3.3a's probe
--   created_at  timestamptz
-- Index: hnsw (embedding vector_cosine_ops), gin on flags.

-- Relationship graph: typed triples, facts are the provenance.
-- ONE ROW PER SUPPORTING FACT (audit P2): the earlier UNIQUE(head,
-- relation,tail) allowed a single provenance, so forgetting that fact
-- cascaded away a relationship still supported by another fact. Reads
-- DISTINCT on (head, relation, tail); a relation disappears only when
-- its LAST supporting row cascades.
CREATE TABLE triple (
  id        uuid PRIMARY KEY,
  head      text NOT NULL, relation text NOT NULL, tail text NOT NULL,
  fact_id   uuid NOT NULL REFERENCES fact(id) ON DELETE CASCADE,
  UNIQUE (head, relation, tail, fact_id)
);

-- Undo ledger (governance §7 deliverable)
CREATE TABLE action_log (
  id         uuid PRIMARY KEY,
  chat_id    bigint NOT NULL,
  tool       text NOT NULL,               -- reminders.create, calendar.create_event...
  args       jsonb NOT NULL,
  undo_tool  text,                        -- the inverse call, precomputed
  undo_args  jsonb,
  done_at    timestamptz NOT NULL,
  undone_at  timestamptz
);
```

Rules carried over from Phase 2, now schema-enforced:

- **MD files remain the human-reviewable source of truth for facts**
  (plan §5). The memory tree is synced INTO `fact` by a one-way
  importer; review/approve/forget still happen on files; Postgres is
  the retrieval index, never the editing surface. A sync conflict
  resolves in favor of the files.
- **`episode.text` stores the cloud_text twin only** — the privacy
  redaction happens before the row exists, so a future RAG hit can
  never resurface what the twin mechanism scrubbed (email metadata,
  review listings). This extends the "seed-time redaction" invariant
  to the new store by construction.
- **Embeddings are computed locally** (qwen3 embedding head or a
  dedicated local embedder). Episode text already went to the cloud
  once when spoken; the embedding pipeline must not create a SECOND
  standing export path (governance §3: provider changes are policy
  events).

### 2.2 Redis (volatile-by-design)

Owns only state allowed to vanish (plan §3): `_history` buffers,
session summaries backlog, listing caches, confirmation stashes +
nonces, burst/fragment timers. Process memory and small JSON files
migrate here 1:1; a Redis flush must leave Kyraan stale-but-honest,
never wrong (governance §5's unmaintained bar).

Day cost/token counters may CACHE in Redis, but the durable ledger
stays persistent — the budget hard-cap must survive a flush.

**Reminders, tasks, and the cost ledger get real Postgres tables**
(audit P1: v1 promised this without DDL or a migration step). Migration
003: `reminder` and `agent_task` tables mirroring the JSON stores'
fields exactly (including pending_result, window/interval columns, the
lease/claim fields), plus `cost_ledger(day, key, value)`. Same
flag+parity pattern as facts: `KYRAAN_PROMISES_BACKEND=files|pg`,
byte-level parity check, files stay the fallback until cutover. This is
workplan P3.2d — sequenced with Part 2, before Redis takes anything.

### 2.3 Migration order (each step shippable, soak between)

1. Stand up Postgres+Redis via docker compose (same host, localhost
   only, no exposed ports — nothing new leaves the machine, §0 table
   unchanged).
2. `action_log` + `undo` command (smallest table, biggest owner value —
   governance §7).
3. Fact sync (files → PG) + FTS; `memory_context()` reads PG, files
   remain authority. Cutover check: context output byte-identical on
   the eval set.
4. Episodes + local embedder + RAG recall as a loop TOOL
   (`memory.recall_episodes`) — the model decides when to reach for
   it, same as every other read.
5. Redis takeover of volatile state; delete the JSON shims.
6. Triples, populated by the extraction pass (typed routing per plan
   §3); graph queries join through facts for provenance.
7. Classifier retirement (see §1) — last, once eval proves parity.

## 3. Semantic recall (RAG) — scope and rules

- Retrieval is **hybrid**: pgvector ANN + Postgres FTS, merged with
  recency bias; top-k enters the prompt as clearly labeled
  "[recalled from <date>]" lines. The memory engine's discretion rules
  apply AFTER retrieval — a sensitive-flagged fact's absence discipline
  (overlap≥2) governs episodes too.
- Web search never disambiguates which entity a recall meant (plan key
  rule 3); recall resolves against memory first, always.
- Episodes older than the 90-day chat retention are kept — the episode
  table IS the long-term memory the log rotation deliberately isn't.
- **Forget cascades to episodes** (audit P1: without this, a forgotten
  fact resurrects through recall). "Forget X" does three things: the
  fact deactivates; its triples' rows cascade (per-provenance, §2.1);
  and an episode sweep marks every episode that references the fact
  (fact_refs) OR matches its content (FTS above a fixed threshold) with
  `suppressed_by += fact_id`. Retrieval excludes suppressed episodes
  unconditionally. Suppression is soft (auditable, reversible if the
  forget was a mistake) but the exclusion is hard.
- A person's delete-me request (§1) deletes their episodes by
  participant, honored without debate — hard delete, not suppression.

## 4. Multi-user identity (the real Phase 3 gate)

Required before spouse stage 2 (governance §8). Design:

- **Enrollment is explicit**: a `person` row with their chat_id, stage,
  and consent date. `_owner_private` generalizes to "enrolled private
  chat at stage ≥ read_mostly"; unknown chats stay rejected.
- **Visibility WHERE clause** (governance §8 stages 2-3):
  - owner sees: everything except `subject_only` facts of consented
    adults (their private facts route to THEIR review — stage 3).
  - spouse (read_mostly) sees: `shared` facts + facts about herself;
    no home control tools, no extraction from her messages for the
    first month (a per-person `extraction_enabled` flag, default off).
  - parents (read_mostly, later): `shared` only; Q&A + reminders.
- **Conflict resolution** (plan §3): a fact contradicting an existing
  one from a DIFFERENT person does not supersede — both stand, flagged
  `disputed`, surfaced to whoever's review queue owns the subject;
  supersession stays within one reviewer's authority.
- Per-person budgets and DND land in `person` config columns, not
  global config.

## 5. Undo (governance §7 deliverable — early, step 2)

Every write executor logs `(tool, args, undo_tool, undo_args)` to
`action_log` at success time. Two semantics fixed by the audit:

- **`undo` means the LAST action, period** (audit P1: skipping to the
  last UNDOABLE action would silently reverse an older action than the
  one the user is looking at). If the newest action is irreversible,
  `undo` says so honestly and does NOT walk back: "Your last action was
  sending the brief — that can't be undone. The action before it
  (reminder 'call mom') can: say 'undo the reminder' to target it."
  Targeted forms ("undo the reminder") reach past the head explicitly.
- **Inverses capture PRIOR STATE at execution time** (audit P1):
  home.turn_on reads the switch state BEFORE acting; its undo restores
  that observed state, and if the device was already in the requested
  state the row logs `undo_tool = NULL` ("no change was made"). The
  UNDO_MAP signature is `(args, result, prior) -> inverse | None`.

One level deep by design; the confirm gate keeps it safe.

## 5a. Advisor personas (optional scope, AFTER the engineering gate)

Consultable specialists (workplan P3.8) are deliberately OUTSIDE the
Phase 3 exit bar (audit P2: they were workplan-only scope). Their trust
boundary, stated here so the architecture owns it: `advisor.consult` is
a READ tool — a persona call gets doctrine + domain-filtered context
(facts/episodes passing the §4 visibility AND exposure filters for the
requesting viewer/tier), makes ONE model call (per-persona model choice
honored; a non-default provider is a §0-table policy event per
governance §3), and returns text. Personas hold no tools, no memory
writes, no conversation state. Hard lines (e.g. wealth: no personalized
recommendations) are deterministic refusals in the executor. None of
this starts before ENGINEERING-DONE.

## 6. What Phase 3 explicitly does NOT do

- No Work agent, no company data (governance §2 — three conditions
  unmet).
- No auto-applied prompt tuning (governance §6 — critic stays
  proposals-only).
- No always-on voice (governance §4 — blocked until Phase 5's own
  consent round).
- No cloud embedding/vector service — retrieval infra is local by
  construction, keeping §0's table unchanged.

## 7. Exit gates — engineering vs rollout (audit P2: one bar conflated
three different clocks)

**ENGINEERING-DONE** (this build's own bar): eval gate green on the
PG-backed context path; the full property-test suite still pinning
every Phase-2 invariant (privacy twins, delivery honesty, receipt
integrity) against the new stores; `undo` live; reminders/tasks durable
in PG with parity proven; backup covers PG (pg_dump in the nightly tar,
restore DRILLED once); spouse stage-2 technically possible behind her
consent flag; zero active facts with subject_reviewed=false. Soak
rhythm during the build: ≥3 clean days per backend flag before its
cutover, ≥1 quiet week between Parts.

**ROLLOUT-APPROVED** (governance §8's calendar gate, independent of
engineering): stage-1's 30 clean soak days elapsed AND the §1 consent
recorded. Engineering can finish first and wait; the calendar can
elapse first and wait — neither substitutes for the other.

All schema changes ship as versioned migrations
(`migrations/00N_*.sql`, applied in order, recorded in
`schema_version`) — v1 is person/fact/triple/action_log; 002 episodes
(post-embedder); 003 reminders/tasks/ledger.
