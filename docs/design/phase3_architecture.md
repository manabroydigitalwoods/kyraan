# Phase 3 Architecture — Datastore, Recall, Multi-User

Status: DRAFT v1 (2026-08-27). Conforms to docs/governance.md (ACCEPTED
2026-08-27) — every design below cites the governance section that
constrains it. Prerequisite per plan.md §5: governance accepted ✅.

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
  id           uuid PRIMARY KEY,
  subject      text NOT NULL REFERENCES person(id),  -- who it is ABOUT
  owner        text NOT NULL REFERENCES person(id),  -- whose review approved it
  content      text NOT NULL,
  kind         text NOT NULL,             -- mirror of memory engine kinds
  flags        text[] NOT NULL DEFAULT '{}',  -- health/safety/fun/sensitive/...
  era          text, sphere  text,
  visibility   text NOT NULL DEFAULT 'owner', -- §4: owner|shared|subject_only
  active       boolean NOT NULL DEFAULT true,
  superseded_by uuid REFERENCES fact(id),
  created_at   timestamptz NOT NULL,
  source_msg   text                        -- provenance for undo/audit
);
CREATE INDEX fact_fts ON fact USING gin (to_tsvector('english', content));

-- Episodic recall: conversation chunks, embedded
CREATE TABLE episode (
  id         uuid PRIMARY KEY,
  chat_id    bigint NOT NULL,
  day        date NOT NULL,
  text       text NOT NULL,               -- cloud_text ONLY (privacy twin)
  embedding  vector(1024),
  created_at timestamptz NOT NULL
);
CREATE INDEX episode_ann ON episode
  USING hnsw (embedding vector_cosine_ops);

-- Relationship graph: typed triples, facts are the provenance
CREATE TABLE triple (
  id        uuid PRIMARY KEY,
  head      text NOT NULL, relation text NOT NULL, tail text NOT NULL,
  fact_id   uuid REFERENCES fact(id) ON DELETE CASCADE,
  UNIQUE (head, relation, tail)
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
nonces, cost counters for the day, burst/fragment timers. Process
memory and small JSON files migrate here 1:1; a Redis flush must leave
Kyraan stale-but-honest, never wrong (governance §5's unmaintained
bar). Reminders/tasks stay in Postgres — they are promises, not cache.

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
  "Forget X" cascades: fact → its triples; a person's delete-me request
  (§1) deletes their episodes by chat_id, honored without debate.

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
`action_log` at success time. `undo` (bare word, no argument) shows
the last undoable action and its inverse as a normal confirm ask:
"Undo: delete the event 'lunch Friday 1pm' I just created — yes/no?".
One level deep by design; the confirm gate keeps it safe; irreversible
actions (a sent message) log with `undo_tool = NULL` and `undo` says
honestly why it can't.

## 6. What Phase 3 explicitly does NOT do

- No Work agent, no company data (governance §2 — three conditions
  unmet).
- No auto-applied prompt tuning (governance §6 — critic stays
  proposals-only).
- No always-on voice (governance §4 — blocked until Phase 5's own
  consent round).
- No cloud embedding/vector service — retrieval infra is local by
  construction, keeping §0's table unchanged.

## 7. Exit bar

Phase 3 is done when: eval gate green on the PG-backed context path;
327+ property tests still pinning every Phase-2 invariant (privacy
twins, delivery honesty, receipt integrity) against the new stores;
`undo` live; spouse stage-2 technically possible behind her consent
flag — and stage-1's 30 clean soak days (governance §8) elapsed.
