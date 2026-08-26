# Progress

Tracks what's actually been built against [plan.md](plan.md), so status
lives in the repo instead of scattered across chat history. Update this
when a phase milestone lands or a load-bearing decision gets made —
day-to-day commits speak for themselves in `git log`.

## Where we are

**Phases 0–2 are complete and Phase 3 is UNBLOCKED** (as of
2026-08-27): the §3a governance decisions are resolved and recorded in
[governance.md](governance.md) — Phase 3's data model must conform to
it. The model-driven agent loop is the primary brain; the tool registry
(docs/design/tool_registry.md) carries eight tools — Google Calendar
(reads + confirm-gated writes), Gmail (metadata + opt-in local-only
bodies), Home Assistant, web search (self-hosted SearXNG), weather
(Open-Meteo), nearby places (OSM/Google), and live-traffic routes
(Google/TomTom) — plus voice notes, photos with local face recognition,
and location pins in the channel layer. CI runs the 433-test suite on
every push. Two repository-wide audits (15 findings) are fixed with
regression pins. A Phase 3 architecture draft exists (one brain + scoped
contexts, PG/pgvector/Redis, multi-user gates); the family-rollout
30-clean-day soak clock runs from 2026-08-27. Phases 4–5 are not
started (the nightly prompt-critic is Phase 4's seed).

## Goal, restated

A personal assistant you talk to over Telegram that reliably handles
reminders and general Q&A, with every autonomous action gated by a kill
switch and an explicit permission level — trustworthy at a small scope
before any expansion into tools, multi-agent routing, or family rollout.
See [plan.md §1](plan.md#1-vision--design-principles) for the full vision.

## What's built

**Control Plane** (`src/kyraan/control_plane/`)
- Kill switch (`touch KILL_SWITCH`) — halts all skill execution and
  proactive sends, checked before every action
- `config/permissions.yaml` — every skill's permission level (`auto` /
  `confirm`), unlisted skills default to `confirm`
- Structured event logging to `logs/events.jsonl` (every model call, tool
  call, and gate decision)
- DND quiet-hours gating for proactive sends (reminders), never for
  user-initiated chat
- `dnd.local_now()` — single source of truth for "now" in `KYRAAN_TIMEZONE`,
  used everywhere a wall-clock time needs to match what the user meant

**Model Router** (`src/kyraan/model_router/`)
- A provider **registry** in `config/permissions.yaml` — each provider is
  `{kind, api_key_env, base_url?}`; adding a new OpenAI-wire-format gateway
  is a config change only, no code change
- Working providers: `ollama` (local), `groq`, `openrouter`, `opencode`,
  `gemini`, `openai` — all live-verified with real keys
- Two tiers (`cheap`, `frontier`), each independently assigned a
  provider+model; **cheap → local Ollama (`llama3.1:8b`)**,
  **frontier → Groq (`openai/gpt-oss-120b`)** — qa.answer and
  reminders.create call cheap, intent classification calls frontier
  specifically (see below for why the split)
- Retry-with-backoff on transient errors (rate limits, 5xx) — every cloud
  provider tested has hit these live
- Captures latency, token usage, reasoning/"thinking" text, and cost
  (`RoutedResponse`) per call — provider SDKs disagree on field names, so
  any of these can come back `None`/`0` rather than raising
- Cost tracking: a tier can declare `pricing` (USD per 1M tokens, tied to
  the specific model); spend is persisted per-day to
  `data/cost_ledger.json` and `router.call()` refuses to dispatch once
  today's spend hits `cost_monitor.daily_budget_usd` — a restart can't
  reset the cap (`session_cost_usd` remains the per-process number the
  TUI sidebar shows)
- Runtime tier overrides (`config.set_tier_override()`) — repoint a tier at
  a different provider/model for the rest of the process without editing
  `permissions.yaml` or restarting (exposed via the TUI's `/tier` command)

**Intent Normalization** (`src/kyraan/intent/`)
- Classification into `reminders.create/list/cancel`, `qa.answer`, or
  `unknown`, with a confidence score; system prompt has concrete few-shot
  example phrasings per intent (added after live misclassifications, see
  below)
- Hardened against malformed model output: JSON `null` fields, intent
  strings outside the known set — both used to crash or misroute

**Memory** (`src/kyraan/memory/`) — the full Phase 1 loop, wired 2026-08-25
- MD files only (no RAG/graph yet, per plan); tree seeded (owner, work)
  and versioned in git — auditable and reversible, per the plan's
  "trace *why* by reading a memory file" principle
- **Extraction** (`extraction.py`): every message ≥ 8 chars runs a
  conservative extraction pass (the auto-permission `memory.propose`
  skill — kill-switch-gated, logged). Only explicitly *stated*,
  self-contained facts; proposals carry the verbatim source statement
- Check-before-write: extraction proposes into `memory/pending_review/`
  (gitignored), a human promotes or rejects via
  `scripts/review_memory.py` — nothing reaches live memory unreviewed,
  and the reply is annotated "📝 Noted for review" so saving is visible
- **Reads**: qa.answer's prompt carries every live fact
  (`load_all_facts()`, char-capped) plus a rolling per-chat 10-exchange
  conversation window for follow-ups and pronouns
- Fact paths validated against a strict layout at propose *and* promote
  time — closes a traversal hole where a model-generated target like
  `../../.env` could have written outside the memory tree

**Triggers** (`src/kyraan/triggers/`)
- Two proactive triggers: reminders, and the **morning brief** (daily
  07:30, `briefs:` in permissions.yaml) — today's calendar + today's
  reminders, composed deterministically (no model call: a proactive
  message must never hallucinate), kill-switch/DND-gated; blocked briefs
  are skipped and logged, never delivered stale
- Persisted to `data/reminders.json`, scheduled via the channel's own
  event loop (Telegram's JobQueue, or a Textual-app-safe equivalent in the
  dev tools) — gated by DND + kill switch on every fire

**Orchestrator & Channel**
- `src/kyraan/agents/orchestrator.py` — the single Phase 1 orchestrator
  (no agent router yet, that's Phase 3)
- `src/kyraan/channels/telegram_bot.py` — the one channel, owner-only
- Runs as a user launchd agent (`ai.kyraan`, plist in
  `~/Library/LaunchAgents/`) — starts at login, restarts on crash,
  logs to `logs/bot.log`; `logs/events.jsonl` rotates at 5MB into
  timestamped archives (never deleted — it's the audit trail)

**Dev tooling** (not part of the installed package)
- `scripts/chat.py` — a local CLI exercising the real orchestrator without
  needing Telegram credentials; colored output, slash commands
  (`/help /reminders /kill /unkill /clear /quit`)
- `scripts/tui.py` — a full-screen Textual dashboard: session stats
  (including cost vs. daily budget), per-turn provider/model/latency/token
  display, an inline thinking indicator, collapsible "Thought" sections
  for a reasoning model's hidden chain-of-thought, real Markdown rendering
  of replies, `/retry` (resend last message), `/tier` (runtime
  provider/model override), and `/export` (save the transcript)
- `textual-dev` (dev extra) — `textual console` + `textual run --dev` give
  a debugging console and CSS hot-reload for TUI development

**Tests**: 92 passing (`pytest -q`) — kernel gating, DND wraparound, memory
propose/promote/reject, scheduler timezone handling, router provider
dispatch, intent normalization against malformed output, cost-calculation
math, orchestrator error handling (including a pin on the qa.answer
no-memory guard), and headless Textual Pilot tests for the TUI (including
retry, tier override, and transcript export).

**Robustness, from a full live walkthrough (2026-08-25):** every use case
(greetings, identity, real-time-aware Q&A, code generation, reminder
create/list/cancel/fire, kill switch, `/tier`, `/retry`, `/export`, garbled
input) was driven end-to-end through the real TUI. Found and fixed a
critical bug in the process: `orchestrator.handle_message` only caught
three specific exception types, so a malformed reminder datetime from
llama3.2 (a duplicated UTC offset) raised an uncaught exception that broke
the app's ability to handle further input — and worse, the bad record had
already been persisted to `data/reminders.json` before the crash, so every
future startup would have hit it again immediately. Now: a final
`except Exception` catch-all in `handle_message`, a specific clear message
for malformed reminder extraction, validate-before-persist in
`scheduler.create_reminder`, and `scheduler.init()` skips (rather than
crashes on) any already-corrupted persisted record. Also hardened the
qa.answer system prompt against two hallucination patterns caught live:
inventing reminder status/countdown details, and falsely denying a
capability (short-delay reminders) Kyraan actually has.

**The cheap tier (local llama3.2) is not reliable enough for Kyraan's
actual workload — confirmed with evidence, not assumption.** A follow-up
walkthrough kept surfacing misclassified reminder requests. Rather than
patch prompts reactively again, compared cheap against frontier (Groq,
still free) across every structured/factual task in the app: intent
classification (~8/14 vs 14/14 correct on the same phrasings), qa.answer
factual accuracy (asked "what time is it?" with the correct time given
directly in the prompt — cheap wrong in 3/3 tries, frontier right in 3/3),
and reminder datetime extraction (cheap produced malformed JSON and once
embedded prose inside the datetime value itself, corrupting it). All three
call sites now use frontier directly. `router.call_with_escalation()` was
removed — it only escalated on an exception, and a confidently wrong
answer was never one, so it couldn't have caught any of this; with intent
normalization's escalation folded away too, it had no remaining callers.
Local Ollama stays fully configured (a one-line `permissions.yaml` edit or
a `/tier` command away) but isn't primary until a bigger local model
proves reliable enough, or there's a specific reason (cost/offline/
privacy) to prefer it over correctness. A full walkthrough rerun after
this change: every response correct, zero crashes, $0.0000 cost.

**Scored walkthrough evaluation (2026-08-25, later the same day):** a
scripted 21-use-case run through the real orchestrator (real providers,
real scheduler, live reminder fire, kill-switch engage/disengage, garbled
input, impossible datetimes) scored **19/21 = 90.5%** — 100% within
Phase 1's claimed scope, zero crashes. The only two failures were the
memory gap: "remember that my wife's name is Mira" persisted nothing, and
a follow-up recall had no context. Worse, qa.answer replied *"Got it—I've
noted that"* while saving nothing — a false claim of a capability Kyraan
doesn't have yet, the mirror image of the false-denial hallucination fixed
earlier. Fixed with a third prompt guard: the system prompt now states
Kyraan cannot store facts or remember across messages, and must never
imply a fact was saved. Live-verified honest answers after the fix.

**Local is viable again — llama3.1:8b (2026-08-25, later still):** pulled
the 8B model (4.9GB, fits the dev machine comfortably) and reran the exact
comparison that justified moving everything to frontier. Results: 12-13/14
correct on intent classification (vs llama3.2's ~8/14, frontier's 14/14 —
one "miss" was actually correct JSON wrapped in a markdown fence the
parser doesn't strip yet, a general robustness gap independent of the
model), 3/3 exact on qa.answer time accuracy (matching frontier exactly,
vs llama3.2's 0/3), 4/4 clean and correct on reminder extraction (matching
frontier exactly, vs llama3.2's malformed JSON/corrupted datetimes). Since
llama3.1:8b matches frontier on two of three tasks, cheap tier now points
at it and qa.answer/reminders.create call `tier="cheap"` again — cutting
Groq call volume and cloud dependency for the two highest-frequency call
sites. Intent classification stays on frontier specifically, still the
measurably more reliable of the two there. Full walkthrough rerun clean:
every response correct, zero crashes, $0.0000 cost, 48/48 tests pass.

**First real Telegram run (2026-08-25, ~14:25 IST): Phase 1's stated goal
is met.** Bot created via BotFather (`@kyraan_assistant_bot`), owner id
taken from the first `getUpdates`, both wired into `.env`, and
`python -m kyraan.main` run for the first time. The owner drove a live
session over real Telegram: greetings, Q&A, reminders list/create — every
event in `logs/events.jsonl` clean (`ok=True` throughout, all skill calls
gated `auto` through the kernel), pending pre-startup messages processed
on boot as expected. The dev harnesses and the real channel share the
identical orchestrator path, and that held: nothing behaved differently
over Telegram. Post-run hygiene to remember: the token was pasted into a
chat during setup, so revoke + reissue via BotFather (`/revoke`) is
prudent; `/setjoingroups` → Disable keeps the bot strictly personal.

## Key decisions made

- **Stack**: Python, self-hosted (not cloud VM) — see `pyproject.toml`
- **Datastore layer (decided 2026-08-25, enters Phase 3)**: Postgres +
  pgvector for everything durable (facts, RAG, full-text, triples) and
  Redis for volatile session state (short-term conversation memory, cost
  counters, ephemeral queues) — now explicit in plan.md §3/§5. Until
  Phase 3, both roles are served by process memory + JSON/MD files, which
  is deliberate: no daemons to maintain during the manual-review weeks
- **Model providers**: hybrid as of 2026-08-25 — local Ollama (`llama3.1:8b`)
  for qa.answer/reminders.create, Groq for intent classification — after
  live-testing ruled out OpenCode Zen (account-wide rate limit trips fast),
  Gemini's free tier (hard 20 requests/day cap), and llama3.2 (the smaller
  3B model, not reliable enough for structured/factual tasks) for real use
- Real API keys for Gemini, OpenAI, OpenCode, Groq, and OpenRouter are in
  the local `.env` (gitignored, never committed) — Anthropic's is not yet
  obtained. Swapping a tier's provider is a one-line config edit, or a
  `/tier` command in the TUI for a temporary session-only switch

## Known limitations / not yet done

- Conversation history is in-memory and per-process — a restart forgets
  the session (durable facts are the memory tree's job, but "what did we
  just talk about?" won't survive a restart)
- Extraction quality depends on the cheap local model — it reliably finds
  clearly stated facts but can phrase them tersely; the human review step
  is the quality gate, and every proposal carries the verbatim source
  statement for exactly that reason
- Memory reads load the whole tree into the prompt each call — fine at
  Phase 1 scale, needs RAG (Phase 3) once the tree outgrows the char cap
- `cancel_reminder`'s cancel path is best-effort in both dev harnesses (an
  already-scheduled asyncio task still fires even if the record is
  cancelled) — fine for dev, not for production
- The confirm flow's pending state is in-memory only — a restart drops an
  unanswered confirmation (fails safe: the action just doesn't run)
- No CI (tests only run when run by hand)
- Budget alert at `alert_threshold_pct` warns in-reply once per day
  (marker persisted in the cost ledger); the hard stop caps spend at 100%
- Section 3a governance gaps (family consent, work/personal data boundary,
  third-party data exposure policy) are **unresolved** and block Phase 3 —
  nothing here should be rolled out to family members yet
- **Tool #2: Home Assistant** (2026-08-25 evening) — HA runs in Docker on
  this Mac (Tapo ecosystem: P110 plugs, H100 hub + T310 sensor, RV30
  vacuum). v1 scope by owner decision: the bedroom AC plug only — state +
  power/energy as auto reads ("is the AC on?" answers with live watts),
  on/off confirm-gated per action, hard entity allowlist in
  permissions.yaml (unlisted entities don't exist for Kyraan; heater/
  geyser/vacuum join later deliberately). Morning brief notes when the AC
  is running. All containers are compose-managed from `docker/` in this
  repo (2026-08-26): HA's /config lives at `docker/homeassistant` (YAML
  tracked, state gitignored — never `git clean -fdx`), SearXNG's config
  at `docker/searxng` (fully tracked, secret via `docker/.env`). HA
  migration path to the Intel MacBook: copy the repo directory including
  untracked state, `docker compose up -d`, update HASS_URL
- Calendar writes are LIVE (2026-08-25 16:33 IST): OAuth ceremony done,
  first real event created over Telegram through the full confirm gate —
  ask named the exact event, "yes" wrote it, audit log clean at both the
  skill and tool level. Tool #1 (Google Calendar) is complete: reads +
  confirm-gated writes, both live-proven
- `calendar.list` is untested against a real Google ICS feed until
  `GOOGLE_CALENDAR_ICS_URL` is set in `.env` (owner setup, see design doc)
- Phase 2 engineering is COMPLETE at family scope (2026-08-25 night):
  loop rails (8-step cap + repeat detection, kernel-enforced), MCP-stdio
  transport (real JSON-RPC client, tested against a stdio server), and a
  live outage drill (HA stopped: honest surfaced error after retries; HA
  restarted: full recovery). Remaining Phase 2 scope is user-side (Tapo
  fleet re-provisioning) and the §3a fork (work tools in or out)
- No RAG, relationship graph, agent router, reflection loop, or curiosity
  queue — Phase 3+, not started
- Intent classification prefers Groq but degrades automatically to the
  local cheap tier on a provider failure (measured 12-13/14 there) —
  the single-point dependency is closed
**Tool #4: web search (2026-08-26):** `web.search` via a self-hosted
SearXNG container (open source, Docker on this Mac like HA, port 8888,
localhost-bound; config in `~/searxng/settings.yml` with the JSON API
enabled) — no API key, no per-query cost, and only the query itself
leaves the machine (Brave's API was considered first but its "free" tier
wants a card). Titles/URLs/snippets only, never full pages; read-only in
the registry, exposed in the agent loop's menu, `SEARXNG_URL` in `.env`.
Injection safety is deterministic, not prompt-hoped: once any web text
enters a turn, the loop's new taint rail locks every non-read tool for the
rest of that turn (logged as `web_taint_blocked_tool`) — a snippet crafted
to say "remind the owner..." cannot reach even an auto-permission write.
The capability brief is conditional now: without `SEARXNG_URL` the hard
"NO INTERNET ACCESS" truth stands verbatim; with it, internet access is
described as exactly search snippets (no pages, no links), so the honesty
guard survives the new ability.

**First web-search soak review (2026-08-26 evening), from the live chat
log:** weather/PM/person-search answers all searched, cited, and read
well. Four fixes out of it: (1) tests were leaking into production state —
26 chat-0/91/92 reminder records purged from `data/reminders.json`
(backup kept), and conftest now isolates the reminder/task/cost stores so
no test can write them again (verified by checksum across a suite run);
(2) a confirmed replay that re-raises ConfirmationRequired now re-asks
honestly instead of falling to the catch-all's "Something went wrong"
about an action that may have run (the live chat-90 failure was pre-fix
code; the guard + regression test close the hole for good); (3) the tool
spec now demands a search for any public figure's CURRENT role ("who is
Mamata?" was answered from stale training data while the PM question
searched); (4) style rule: web answers lead with the answer in metric/
local units then one Source line — an AccuWeather snippet had been
relayed as °F links-first.

**Location pins (2026-08-26 night):** a shared Telegram location matched
no handler and was silently dropped (live: the model kept asking "which
area are you in?" while the pin sat unread). Now `filters.LOCATION` →
reverse geocode via OSM Nominatim (`channels/location.py`, no key; the
coordinates go out only for a pin the owner chose to share, and any
geocoder failure falls back to raw coordinates) → the pin enters the
normal pipeline as "[I'm sharing my current location: <place> (lat,
lon)]", bursting with any caption into one thought. The capability brief
now tells the model to USE an arrived pin and that it can never request
or track location. Live-verified against real Nominatim (Kolkata and
Gajoldoba pins resolve correctly).

**Tool #5: weather (2026-08-26 night):** `weather.get` via Open-Meteo
(free, keyless) — current conditions + 3-day forecast, structured, by
place name (their geocoder) or exact lat/lon from a shared pin. Built
because the soak showed weather-by-web-search failing structurally: five
coordinate-stuffed queries in a row (search engines match none), the
step cap burned, the fallback tier rescuing the turn, and a 10-day
forecast snippet glossed as "currently sunny" at 8 PM. The search-query
doctrine also hardened in the agent prompt: no coordinates in queries,
broaden to the next-larger place yourself instead of asking the user,
one broadened retry then answer honestly; forecast data is labeled
forecast. Live-verified end-to-end: the model picks weather.get for a
pin, keeps now/forecast straight, °C. Open-Meteo's real answer for the
pin (27°C, thunderstorms coming) contradicted the earlier snippet answer
("sunny 32°C") — the structural fix earned its keep on day one.

**Tool #6: nearby places (2026-08-26 night):** `places.nearby` — hospital/
pharmacy/atm/bank/restaurant/cafe/hotel/sightseeing/fuel/police/grocery
around a shared pin or named place, distance-sorted, each result with a
keyless Google Maps link that opens navigation on tap. Two backends in
one adapter: OpenStreetMap Overpass (free, keyless, the default — needs
the identifying UA + form Content-Type or the public instance answers
406, and stalls transiently under load, which the registry's retries
absorb) and Google Places API (New), auto-selected when
GOOGLE_MAPS_API_KEY is set — the quality upgrade (ratings, open-now) the
owner can opt into later since it needs GCP billing/card. Empty results
at the default 3 km auto-widen once to 10 km (rural reality: zero
hospitals mapped within 3 km of the owner's pin) before an honest
sparse-data note. Coordinates normalize to 4 decimals in the executor,
same as weather. Live-verified end-to-end: real hospitals near the
owner's pin with distances and map links, ATMs by place name.

**Tool #7: travel times / traffic (2026-08-26 night):** `routes.eta` via
the Google Routes API (same GOOGLE_MAPS_API_KEY, Routes API enabled on
the project) — distance + duration with live traffic vs free-flow
between any two endpoints, each a place name (Google geocodes it) or the
pin's lat/lon; drive/two_wheeler/walk. The traffic report IS the
duration delta ("52 min right now, ~49 usual, delay ~3 — light"). No
keyless fallback by design: live traffic exists only at
Google/TomTom/HERE, and a silently traffic-blind ETA would be a lie —
the tool spec orders the model to say so rather than estimate.
Menu-gated + brief-gated on the key like web.search. Also covers plain
"distance from X to Y" questions. Live-verified end-to-end (model probe:
correct now-vs-usual phrasing on the real Radhabari→Jalpaiguri route).

**Place resolution + TomTom fallback (2026-08-26 late night):** the live
log showed "from siliguri, city center" answered with a request for
lat/lon — twice. Root cause turned out to be the tool spec's params
example LEADING with the lat/lon form (a small model imitates the first
form shown; three prompt-rule escalations failed before this was
spotted): the example now leads with free-text names ("City Center Mall,
Siliguri") and states coordinates are never required. The deflection
guard grew coordinate-homework patterns (pin/lat-lon asks, "do you
mean...?" echoes, "exact spot/landmark") and now corrects up to TWO
drafts per turn — the third stands as genuine. Probes: slangy endpoints
("city center mall") now resolve and answer in one call, with the
interpretation stated. routes.eta also gained a TomTom fallback (owner's
key, free tier, no card): Google fails → TomTom answers, both with real
live traffic, `source` marks the degradation; TomTom alone can serve as
primary. The no-traffic-blind-ETA rule is unchanged — both backends
carry live traffic, and if every backend fails the model must say so,
never estimate. Live-verifying TomTom took three rounds: its /geocode
endpoint is addresses-only and resolved "City Center Mall, Siliguri" to
Ohio, USA (then, country-biased, to a same-named mall 1671 km away) —
the fix is the /search fuzzy endpoint (POIs included) plus a
KYRAAN_HOME_COUNTRY=IN bias and pin-coordinate bias when available; a
routing 400 now reads "no drivable route — an endpoint may have resolved
to the wrong place". Verified: the real 51.6 km route.

**Flow tracing + prompt report (2026-08-26 night):** every user message
now opens a TURN — a contextvar id stamped on every event, so the whole
flow (user text → each model decision → each tool call → reply)
reconstructs with one grep. Full prompt/response text goes to
`logs/traces.jsonl` (same rotation/permissions as events; local-disk
§3a boundary), tool results carry `duration_ms`, turns log
`turn_start`/`turn_end` with total wall time. `scripts/trace.py`
pretty-prints one turn; `scripts/prompt_report.py` measures the real
assembled prompt (section sizes), cache health from the day's calls,
dead tool references, and near-duplicate rules — REPORT ONLY, per §6:
prompt edits stay human and gate on scripts/eval.py. The deliberate
non-build: an automatic prompt rewriter — nearly every prompt line is a
cited live failure, the static prefix's byte-stability is worth ~90% on
input cost, and today's coordinate bug came from example ORDER, which no
linter measures. First report immediately found: 981/1483 frontier calls
today were full cache misses (expected on a day with ~15 prompt-editing
deploys — recheck on a quiet day), and one duplicated usage-report rule.

**5a-5c (2026-08-26, last block of the day):** (a) the nightly
self-review gained a PROMPT CRITIC (`self_review.prompt_critic`): a
deterministic digest of the day's guard firings, tool failures, and
cache/latency stats plus the STATIC prompt sections (never memory or
pending facts) goes to the frontier model for at most 3 proposed prompt
edits with evidence — proposals only, edits stay human, gated on
scripts/eval.py; a critic crash never sinks the review. (b) email bodies
local-only, built but OFF until the owner opts in: KYRAAN_EMAIL_BODIES=
local in .env → re-run setup_google_oauth.py (requests gmail.readonly
instead of the metadata-only scope) → restart. email.read fetches unread
bodies (query-filterable), the executor summarizes them with the LOCAL
model and short-circuits the reply — content never enters a cloud prompt
or history, and the executor refuses outright if the cheap tier isn't
local. (c) CI: .github/workflows/tests.yml runs the full suite on every
push/PR — no secrets in CI by design (tests fake all providers).

**§3a draft (2026-08-26, session close):** docs/governance.md — proposed
answers for every §3a gap (consent, work boundary, third-party exposure,
voice, maintenance, review scaling, undo, staged rollout), grounded in a
what-leaves-the-machine table of the live system. Roles only, no names
(the doc is tracked; the PII scrub stands). Each section ends in a
Decide: line — the owner's red pen makes it ACCEPTED, which unblocks
Phase 3.

**Ops hardening (2026-08-27 night):** eval gate extended to the week's
tool surfaces (weather/routes/places/search as SOFT — an upstream outage
must not redden the gate — and the deterministic faces path as HARD;
photo turns bypass handle_message and stay manually tested). Nightly
state backup: `scripts/backup.py` via the `ai.kyraan.backup` launchd
agent (03:30, tars data/+memory/+config+.env to ~/Backups/kyraan or
KYRAAN_BACKUP_DIR, keep-14) — reverses the earlier skip-backup decision
now that face templates and the memory index live in data/. Memory
hygiene done via the engine: the stray "Born: 5 January 1955" fact and
two of three duplicate evening-routine facts deactivated. Known and
ACCEPTED latency floor: every tool turn costs two frontier decisions
(~3.7s) — the levers (native tool-calling API, streamed replies) are
Phase 3-era work, recorded here so the ceiling is a choice.

**Parallel-session protocol (2026-08-27):** two Claude sessions share
this repo (one owns Phase 3 design, one owns soak/ops). Rules learned
the hard way: never deploy (`launchctl kickstart`) while the other may
be mid-edit — a watchdog respawn once served MIXED half-edited code
live; treat a surprise red test run as possible mid-edit interference
and re-run before debugging; commits are the sync points.

## Next steps

1. Phase 3 build, from the architecture draft — governance.md is the
   constraint set; `undo` is a committed deliverable (§7)
2. Owner hygiene still owed (the ONE open thread): bot-token
   revoke/reissue (it passed through a chat during setup;
   `/setjoingroups` → Disable), and the ICS-URL/client-secret/HASS-token
   rotations — plus re-enrolling the child's face from 3-4 clear photos
   now that match scores are logged
3. Keep the memory-review cadence (in-chat "review memory" or
   `scripts/review_memory.py`) — §6's sample-review gate needs 200
   reviewed at ≥90% trailing approval
4. Watch the nightly self-review's prompt critiques and the cache-health
   reading on a quiet day (`scripts/prompt_report.py`)
5. Family stage-2 prep: nothing ships before ≈26 Sep (30 clean soak
   days) AND the Phase 3 multi-user gates (per-person visibility,
   conflict resolution) exist
