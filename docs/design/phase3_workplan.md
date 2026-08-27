# Phase 3 Work Plan — precise, shippable slices

Status: v2 (2026-08-27, revised with the architecture after the
16-finding design audit). Slices phase3_architecture.md's migration order
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

**P3.0b — Store scaffolding + migration 001.** New `src/kyraan/store/pg.py`
(one connection pool; DSN from `KYRAAN_PG_DSN`, default localhost) and
`migrations/001_core.sql` — person/fact/triple/action_log ONLY (episodes
wait for the embedder's dimension in 002, promises land in 003; audit
P2). `scripts/migrate.py` applies numbered migrations in order and
records them in `schema_version`. Also HERE, not later (audit
durability gap): `scripts/backup.py` gains `pg_dump` into the nightly
tar the day PG holds its first row, and `scripts/restore.py` restores a
named backup into a fresh PG — with a drill in the Done-when, because a
backup nobody has restored is a hope, not a backup. Add `psycopg[binary]` + `redis` to pyproject. Tests: a `pg` pytest
marker — `tests/test_store_pg.py` connects, migrates into a throwaway
schema, asserts tables exist; marker auto-skips when PG is unreachable
(dev without containers, and CI until P3.0c). **Done when:** migrate is
rerunnable with no diff; suite green with and without PG running; a
backup taken, restored into a scratch PG, and row-count-verified once.

**P3.0c — CI services.** Add postgres+redis service containers to
.github/workflows/tests.yml on ONE matrix leg; the `pg` marker runs
there, skips elsewhere. **Done when:** CI green with the pg tests
actually executed (assert on the run log, not just green).

## Part 1 — Undo (arch §5; governance §7 — early, highest owner value)

**P3.1a — action_log module.** `store/actions.py`: `record(chat_id,
tool, args, undo_tool, undo_args)`, `last_action(chat_id)` — the NEWEST
action, undoable or not (audit P1: skipping irreversible heads would
silently reverse an OLDER action than the one the user means), plus
`last_action_of(chat_id, tool_prefix)` for targeted forms ("undo the
reminder"), and `mark_undone(id)`. Tests (pg marker): round-trip;
irreversible head returned as-is; targeted lookup. **Done when:**
module + tests only; nothing calls it yet.

**P3.1b — writes declare their inverses.** One dict in loop_tools:
`UNDO_MAP: {tool: (args, result, prior) -> (undo_tool, undo_args) | None}`
— `prior` is state observed BEFORE the write where the inverse needs it
(audit P1): home switches read home.get_state first, undo restores the
OBSERVED prior state, and an already-in-that-state write logs None ("no
change was made"). Mapping:
precisely: `calendar.create_event → calendar.delete_event {event_id:
result.id, title}`; `reminders.create → reminders.cancel {reminder_id:
result.id}`; `tasks.schedule → tasks.cancel {task_id: result.id}`;
`faces.remember → faces.forget {name}`; `home.turn_on/off → restore observed prior state, or None if unchanged`; `calendar.delete_event / memory.forget / sends → None`
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
kernel.run_tool (confirmed) → `mark_undone`. An irreversible newest action → honest "your last action (X) can't be
undone" naming the next targetable one — undo NEVER silently reaches
past the head (audit P1); targeted "undo the reminder" reaches
explicitly.
Tests: ask wording, yes-path executes the stashed inverse
byte-identically, no-path, empty-log path. Eval: one HARD two-step case
(create reminder → undo → ask → yes → gone). **Done when:** live over
Telegram: create an event, say undo, confirm, event gone.

## Part 2 — Facts → Postgres (arch §2.3 step 3)

**P3.2a — person + fact sync (write path).** Seed `person('owner')`.
`store/facts.py`: `upsert_from_entry(entry)` with deterministic identity
`id = uuid5(KYRAAN_NS, legacy_id)` (idempotent resync, audit P1) and
SUBJECT DERIVATION (audit P1 — blanket subject='owner' mis-owns family
facts): the memory tree's `people/<name>` path names the subject where a
person row exists for it; everything unresolved lands subject='owner'
with `subject_reviewed=false`. `scripts/review_subjects.py` lists the
unreviewed for the owner to assign; P3.5a's gate refuses any non-owner
viewer while one remains. visibility='owner', exposure='cloud_ok' for
all migrated facts (governance §3's no-pull-backs decision). Hook the THREE mutation points in memory.engine —
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

**P3.2d — promises to Postgres (arch §2.2 / migration 003).** `reminder`
+ `agent_task` tables mirroring the JSON stores field-for-field
(pending_result, interval/window, lease fields included) +
`cost_ledger`. `KYRAAN_PROMISES_BACKEND=files|pg` (default files), a
parity harness diffing both stores after each mutation on the eval
sequence, cutover after 3 clean flag-days. Backup already covers PG
(P3.0b). Tests: every store mutation runs against both backends
(parametrized); crash-window semantics (claim/lease) proven on pg.
**Done when:** parity clean for 3 days and a restart-with-pg-only
serves every pending reminder correctly.

## Part 3 — Episodes + RAG (arch §2.3 step 4, §3)

**P3.3a — local embedder probe.** Pick the embedding model (Ollama
`/api/embed`, e.g. `nomic-embed-text` 768-d or `qwen3-embedding` —
whichever the probe proves on this Mac). `store/embed.py`:
`embed(texts) -> vectors`, LOCAL-ONLY guarantee (refuses if the resolved
Ollama endpoint isn't local — reuse router.provider_is_local). The episode DDL ships HERE as `migrations/002_episodes.sql` with the
probe-pinned dimension and the full column model from arch §2.1
(participants, visibility, exposure, flags, fact_refs, suppressed_by)
— schema v1 deliberately shipped without it (audit P2). Tests: dimension pin; locality refusal. **Done when:** probe
script embeds and round-trips a similarity sanity check (cat~kitten >
cat~carburetor).

**P3.3b — episode writer + backfill.** Nightly job (alongside
self-review): chunk the day's `cloud_text` transcript per chat into
~10-exchange episodes → LOCAL cheap-tier sensitivity tagging (flags
column — the discretion rules must be enforceable on episodes, audit
P1) → embed → insert with participants/visibility/exposure set. `scripts/backfill_episodes.py`
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

**P3.3d — forget cascades to episodes (arch §3; audit P1).** Forgetting
a fact sweeps episodes: fact_refs hits + FTS matches above the fixed
threshold get `suppressed_by += fact_id`; ALL retrieval paths exclude
suppressed episodes; a person's delete-me hard-deletes their episodes by
participant. Tests: forget → matching episode unfindable via recall;
suppression is auditable; delete-me removes rows. Eval: forget a seeded
fact, then a recall probe must NOT resurface it. **Done when:** the
resurrection eval case passes.

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
Enablement gate (audit P1): enrolling any non-owner person REFUSES while
an active fact has subject_reviewed=false — mis-owned facts must be
assigned before a second viewer exists. Tests: unknown chat rejected;
enrolled read_mostly accepted; stage 'none' rejected; the gate refusal.
**Done when:** a second (test) chat_id can talk to the bot in
read_mostly and the owner path is unchanged.

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

**P3.5e — review scaling per governance §6 (audit P2: accepted policy,
no work package).** Counters (total reviewed, trailing-50 approval rate)
persisted with the review flow; when 200/≥90% is met, every-3rd
proposals hold for review and the rest auto-approve after a 24h
objection window (visible as "awaiting" exactly as today); full-review
re-triggers on a wrong auto-approval, a tier change, or an
extraction-prompt change (each already a loggable event). Tests: the
threshold math; the 24h window; each retrigger. **Done when:** the
counters run live and the mode flips only when the policy says so.

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

## Part 8 — Advisor personas (OPTIONAL — outside the exit bar, after ENGINEERING-DONE)

Scope status (audit P2): P3.8 is post-exit optional scope, now owned by
arch §5a (trust boundary: read-only, no tools, no state; hard lines are
deterministic executor refusals; a non-default persona model is a
§0-table policy event). It contributes nothing to, and cannot block,
the Phase 3 exit gates.

Expertise without a router: specialists are deep persona doctrines paid
for only when consulted, not agents that own conversations. The main
loop keeps the gates, history, and memory; `advisor.consult` is a read
tool like any other. (Decision record: a routed multi-agent design was
considered twice and rejected — dispatch layers lost to the single loop
in Phase 2's live evidence, and expertise comes from prompt + curated
context + risk posture + model choice, none of which need agenthood.
Revisit ONLY for true parallel workloads or separate trust domains.)

**P3.8a — persona spec + the consult executor.** `personas/<name>.md`
(doctrine, hard lines, model override, context filter spec) +
`advisor.consult {domain, question}` executor: assemble persona doctrine
+ domain-filtered facts (health → [HEALTH]-flagged) + relevant episodes
→ ONE model call (per-persona model choice honored) → labeled advisory
answer back to the loop. Tests: context filter selects only flagged
facts; persona model override; the label. **Done when:** a health
question answers with health-only context and the defer-to-doctors line.

**P3.8b — health persona live.** Doctrine: cite what it knows from YOUR
facts, hedge, always defer to doctors for diagnosis/dosage; never
volunteer sensitive facts outside a directly-health turn (the existing
discretion rule, restated for the persona). Eval: soft quality case +
HARD case pinning the defer line on a diagnosis-shaped question.
**Done when:** live over Telegram with a real health question.

**P3.8c — legal + wealth personas, wealth hard-lined.** Legal:
concepts-not-counsel, jurisdiction-aware (India). Wealth: financial
literacy and budgeting ONLY — a personalized-recommendation ask ("what
stock should I buy") gets a deterministic refusal in the executor, not
just doctrine (same class as the taint rail). Business persona is NOT
built — it is the deferred Work agent (governance §2) under another
name. Tests + a HARD eval case on the wealth refusal. **Done when:**
the wealth refusal survives rephrasing probes.

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
P3.0a → P3.0b → P3.0c                        (001 + backup/restore drill)
         └→ P3.1a → P3.1b → P3.1c            (undo live early)
         └→ P3.2a → P3.2b → P3.2c → P3.2d    (facts, then promises, to PG)
                     └→ P3.3a → P3.3b → P3.3c → P3.3d  (RAG + forget cascade)
soak ─── P3.4a → P3.4b → P3.4c               (Redis, after Part 2 proves PG ops)
         P3.5a → P3.5b → P3.5c → P3.5d → P3.5e  (multi-user; 5a needs P3.2a's
                                              subject review CLEAN)
         P3.6a → P3.6b                        (graph; needs P3.2)
ENGINEERING-DONE gate (arch §7) ──────────────
         P3.7a → P3.7b                        (retirement)
         P3.8a → P3.8b → P3.8c               (advisors — optional, post-gate)
ROLLOUT-APPROVED = governance §8 calendar + consent, independent track
```

Exit gates per arch §7: ENGINEERING-DONE is this build's bar;
ROLLOUT-APPROVED (the 30-clean-day clock running since 2026-08-27, plus
§1 consent) is the independent calendar gate — P3.5 makes stage 2
possible; the calendar makes it allowed.
