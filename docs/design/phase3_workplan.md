# Phase 3 Work Plan — precise, shippable slices

Status: v1 (2026-08-27). Slices phase3_architecture.md's migration order
into tickets sized for one sitting each. Every ticket names its files,
its tests, and its **Done when** — a ticket without all three isn't
ready to start. Order within a part is strict; parts may interleave only
where the dependency notes allow. After every ticket: full suite green,
eval gate green, commit; soak days between PARTS, not tickets.

Conventions: new store code lives in `src/kyraan/store/` (pg.py,
redis.py, actions.py, embed.py). Every PG/Redis feature ships behind an
env flag defaulting OFF, cut over only after its parity check.

---

## Part 0 — Infrastructure (arch §2.3 step 1)

**P3.0a — Postgres + Redis containers.** Add `postgres:16` (with
pgvector image `pgvector/pgvector:pg16`) and `redis:7` to
docker/docker-compose.yml — localhost-bound ports (5432/6379), volumes
under docker/ (pgdata gitignored like HA state), healthchecks, password
in docker/.env. Files: docker/docker-compose.yml, docker/.env.example,
.gitignore. Test: none (infra). **Done when:** `docker compose up -d`
brings both healthy; `psql` and `redis-cli ping` succeed from the host;
governance §0 table needs no change (nothing leaves the machine).

**P3.0b — Store scaffolding + schema v1.** New `src/kyraan/store/pg.py`
(one connection pool; DSN from `KYRAAN_PG_DSN`, default localhost) and
`store/schema.sql` (the arch §2.1 DDL verbatim), applied idempotently by
`scripts/migrate.py` (CREATE TABLE IF NOT EXISTS + a `schema_version`
row). Add `psycopg[binary]` + `redis` to pyproject. Tests: a `pg` pytest
marker — `tests/test_store_pg.py` connects, migrates into a throwaway
schema, asserts tables exist; marker auto-skips when PG is unreachable
(dev without containers, and CI until P3.0c). **Done when:** migrate is
rerunnable with no diff; suite green with and without PG running.

**P3.0c — CI services.** Add postgres+redis service containers to
.github/workflows/tests.yml on ONE matrix leg; the `pg` marker runs
there, skips elsewhere. **Done when:** CI green with the pg tests
actually executed (assert on the run log, not just green).

## Part 1 — Undo (arch §5; governance §7 — early, highest owner value)

**P3.1a — action_log module.** `store/actions.py`: `record(chat_id,
tool, args, undo_tool, undo_args)`, `last_undoable(chat_id)`,
`mark_undone(id)` — thin, typed, no business logic. Tests (pg marker):
record→fetch→mark round-trip; NULL undo_tool rows are skipped by
`last_undoable`. **Done when:** module + tests only; nothing calls it
yet.

**P3.1b — writes declare their inverses.** One dict in loop_tools:
`UNDO_MAP: {tool: (args, result) -> (undo_tool, undo_args) | None}` —
precisely: `calendar.create_event → calendar.delete_event {event_id:
result.id, title}`; `reminders.create → reminders.cancel {reminder_id:
result.id}`; `tasks.schedule → tasks.cancel {task_id: result.id}`;
`faces.remember → faces.forget {name}`; `home.turn_on ↔ home.turn_off
{entity}`; `calendar.delete_event / memory.forget / sends → None`
(logged, not undoable — deletion inverses need payload capture, deferred
to P3.1d if ever). Recording happens at the two success points: the
confirmed-replay handler and auto-permission write executors. Tests: per
tool, execute (faked dispatch) → assert the logged inverse args.
**Done when:** every write in TOOLS produces an action_log row with the
correct inverse or an explicit None.

**P3.1c — the `undo` command.** Deterministic branch in orchestrator
(exact-word `undo`, like forget-face): fetch `last_undoable` → confirm
ask naming the inverse concretely ("Undo: delete the event 'lunch
Friday 1pm' I just created — yes/no") → run the inverse through
kernel.run_tool (confirmed) → `mark_undone`. No undoable action → honest
"nothing to undo (last action: X, which can't be undone because Y)".
Tests: ask wording, yes-path executes the stashed inverse
byte-identically, no-path, empty-log path. Eval: one HARD two-step case
(create reminder → undo → ask → yes → gone). **Done when:** live over
Telegram: create an event, say undo, confirm, event gone.

## Part 2 — Facts → Postgres (arch §2.3 step 3)

**P3.2a — person + fact sync (write path).** Seed `person('owner')`.
`store/facts.py`: `upsert_from_entry(entry)` mapping index.json fields →
`fact` columns (subject='owner', owner='owner', visibility='owner' for
every existing fact). Hook the THREE mutation points in memory.engine —
promote, forget, supersede — to mirror into PG after the file write
(file write first; PG failure logs and never blocks the file — files
remain authority, arch §2.1). Plus `scripts/resync_facts.py`: full
rebuild from index.json, idempotent. Tests: promote→row; forget→
active=false; supersede→superseded_by set; PG-down → file op still
succeeds + `fact_sync_deferred` event. **Done when:** resync then any
review action leaves file and PG in agreement (a checker script asserts
it).

**P3.2b — FTS read path behind a flag.** `memory_context()` (and
build_context's candidate pool) gains `KYRAAN_MEMORY_BACKEND=files|pg`
(default files): pg mode pulls candidates via FTS + flags, then applies
the SAME ranking/discretion code — only candidate retrieval changes.
Parity harness `scripts/compare_memory_backends.py`: run both backends
over the eval prompts + 20 recent real messages; report any差 output.
Tests: flag routing; pg-down in pg mode falls back to files with one
logged event. **Done when:** parity report shows byte-identical context
on every probe.

**P3.2c — read cutover.** Flip default to pg after ≥3 clean soak days
on the flag + green eval. Files stay the write/review surface
indefinitely. **Done when:** a soak week passes with `fact_sync_*`
events clean; the files→PG direction never reversed.

## Part 3 — Episodes + RAG (arch §2.3 step 4, §3)

**P3.3a — local embedder probe.** Pick the embedding model (Ollama
`/api/embed`, e.g. `nomic-embed-text` 768-d or `qwen3-embedding` —
whichever the probe proves on this Mac). `store/embed.py`:
`embed(texts) -> vectors`, LOCAL-ONLY guarantee (refuses if the resolved
Ollama endpoint isn't local — reuse router.provider_is_local). Adjust
`vector(N)` in schema.sql to the chosen dimension BEFORE any rows
exist. Tests: dimension pin; locality refusal. **Done when:** probe
script embeds and round-trips a similarity sanity check (cat~kitten >
cat~carburetor).

**P3.3b — episode writer + backfill.** Nightly job (alongside
self-review): chunk the day's `cloud_text` transcript per chat into
~10-exchange episodes → embed → insert. `scripts/backfill_episodes.py`
replays chat.jsonl history (cloud_text ONLY — the privacy twin rule is
enforced by reading that field, arch §2.1). Tests: chunker; twin-field
selection (a record with cloud_text must never embed raw text);
idempotent backfill. **Done when:** backfill runs over the real log;
row count and a spot-check reported.

**P3.3c — recall as a loop tool.** `memory.recall_episodes` (read-only,
auto): hybrid ANN+FTS merged with recency bias, top-k, each line labeled
"[recalled from <date>]"; discretion rules applied post-retrieval
(sensitive-flag absence discipline, arch §3). Menu entry teaches WHEN
("what did we discuss about X last month"). Tests: executor with faked
store; label format; k cap. Eval: one soft recall case. **Done when:**
a live "what did I tell you about <old topic>?" answers from an episode
older than the history window.

## Part 4 — Redis takeover (arch §2.3 step 5) — after Parts 1-2 soak

**P3.4a — session state.** session.py's `_history`/`_summary_backlog`
move to Redis behind `KYRAAN_SESSION_BACKEND=memory|redis` (default
memory): same module API, Redis lists with per-chat keys, in-memory
fallback + one logged event when Redis is down. Tests: API parity suite
runs against both backends (parametrized fixture). **Done when:** a bot
restart no longer forgets the conversation window (live check), and
Redis-down degrades to exactly today's behavior.

**P3.4b — confirmation stash + nonces + listing cache.** Same pattern,
with TTLs matching today's in-memory lifetimes (5-min confirmations,
10-min listings). This deliberately UPGRADES an invariant: a pending
confirm now survives restart — the orphaned-yes guard's "ask didn't
survive a restart" reply becomes rare instead of routine. Tests: TTL
expiry; nonce integrity across a simulated restart (new process, same
Redis). **Done when:** ask → restart bot → yes still executes the
stashed action byte-identically.

**P3.4c — volatile counters ONLY.** Day cost/token counters may cache
in Redis, but the durable ledger stays file/PG — the budget hard-cap
must survive a Redis flush (correction to arch §2.2's listing;
governance §5's stale-but-honest bar). **Done when:** flushall +
restart still knows today's spend.

## Part 5 — Multi-user identity (arch §4) — the spouse-stage-2 gate

**P3.5a — enrollment + channel gate.** `scripts/enroll_person.py`
(owner-run: person id, chat_id, stage, consent date → person row).
`_owner_private` generalizes to `_authorized(update) -> person|None`
(enrolled private chat at stage ≥ read_mostly; unknown chats rejected
exactly as today). Every downstream chat_id use keyed per person.
Tests: unknown chat rejected; enrolled read_mostly accepted; stage
'none' rejected. **Done when:** a second (test) chat_id can talk to the
bot in read_mostly and the owner path is unchanged.

**P3.5b — tool scopes per stage.** The arch §1 two-layer pattern:
`_tools_block(scope)` filters the menu AND kernel checks a per-person
allowlist (read_mostly = Q&A/reminders/calendar-read/weather/places/
routes/search; NO home control, NO memory writes, NO email). Config:
stage→toolset map in permissions.yaml. Tests: menu contents per stage;
kernel refusal when the model calls an out-of-scope tool anyway.
**Done when:** the read_mostly test chat cannot switch the AC by any
phrasing (eval-style probe).

**P3.5c — visibility + per-person review.** fact queries gain the §4
WHERE clause (owner / shared / subject_only); extraction from
non-owner messages behind per-person `extraction_enabled` (default
false, first-month rule); a spouse-stage-3 fact about herself routes to
HER review queue (review flow keyed by person, not hardcoded owner).
Tests: each visibility row against each viewer; extraction-off proves
no proposal from her messages; review routing. **Done when:** the
matrix test passes and the owner's own flow is byte-identical.

**P3.5d — disputes + per-person config.** Cross-person contradiction →
both facts stand flagged `disputed`, surfaced to the subject-owner's
queue (arch §4); per-person DND windows and daily budget columns
honored by can_send_proactively and the briefs. Tests: dispute path;
per-person DND. **Done when:** a seeded contradiction shows up in the
right queue and neither fact silently wins.

## Part 6 — Relationship graph (arch §2.3 step 6)

**P3.6a — triples from approved facts.** On promote, a cheap-tier pass
extracts typed triples (person→relation→entity) from the APPROVED fact
only; rows carry fact_id provenance; forget cascades (schema already
does). Tests: promote→triples; forget→cascade. **Done when:** the
existing fact tree resyncs into a populated, provenance-linked graph.

**P3.6b — graph read tool.** `memory.relations` ("how is X related to
Y", "who is connected to the school run") — join through triples, cite
the source facts. Menu + tests + one soft eval case. **Done when:** a
live relation question answers with provenance.

## Part 7 — Classifier retirement (arch §1, LAST)

**P3.7a — parity proof.** Run the full eval gate with the frontier tier
disabled (cheap-tier loop only); fix steering until HARD cases pass on
qwen3. **Done when:** two consecutive all-green degraded-mode eval runs.

**P3.7b — deletion.** Remove legacy_handlers.py, the normalize/intent
path, AGENT_LOOP_ENABLED, and their tests; the fallback chain becomes
frontier-loop → cheap-loop → honest error. **Done when:** suite green,
eval green, and orchestrator's _dispatch shrinks to guards + confirm
flow + loop invocation — one brain, two tiers, zero dispatch rules
(arch §1's consequence, delivered).

---

## Sequencing at a glance

```
P3.0a → P3.0b → P3.0c
         └→ P3.1a → P3.1b → P3.1c            (undo live early)
         └→ P3.2a → P3.2b → P3.2c            (facts read from PG)
                     └→ P3.3a → P3.3b → P3.3c (RAG)
soak ─── P3.4a → P3.4b → P3.4c               (Redis, after 1-2 prove PG ops)
         P3.5a → P3.5b → P3.5c → P3.5d       (multi-user; 5a may start any time
                                              after P3.0b — person table only)
         P3.6a → P3.6b                        (graph; needs P3.2)
LAST ─── P3.7a → P3.7b                        (retirement)
```

Exit bar unchanged from arch §7. The 30-clean-day clock (governance §8,
running since 2026-08-27) gates the spouse ROLLOUT, not this build —
P3.5 makes stage 2 possible; the calendar makes it allowed.
