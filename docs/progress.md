# Progress

Tracks what's actually been built against [plan.md](plan.md), so status
lives in the repo instead of scattered across chat history. Update this
when a phase milestone lands or a load-bearing decision gets made —
day-to-day commits speak for themselves in `git log`.

## Where we are

**Phase 0 and Phase 1 are complete; Phase 2 (Tool Integrations) has begun** — built,
iterated, and live-verified on 2026-08-25, including the first real
end-to-end Telegram session and the full memory loop (extraction →
human review → recall) the same day. Phase 2's tool
registry is designed (docs/design/tool_registry.md), built, and carrying
its first tool — read-only Google Calendar. Phases 3–5 are not started.

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
- Runs as a user launchd agent (`io.digitalwoods.kyraan`, plist in
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
  is running. HA migration path to the Intel MacBook: copy
  ~/homeassistant, same container command, update HASS_URL
- Calendar writes are LIVE (2026-08-25 16:33 IST): OAuth ceremony done,
  first real event created over Telegram through the full confirm gate —
  ask named the exact event, "yes" wrote it, audit log clean at both the
  skill and tool level. Tool #1 (Google Calendar) is complete: reads +
  confirm-gated writes, both live-proven
- `calendar.list` is untested against a real Google ICS feed until
  `GOOGLE_CALENDAR_ICS_URL` is set in `.env` (owner setup, see design doc)
- No RAG, relationship graph, agent router, reflection loop, or curiosity
  queue — Phase 3+, not started
- Intent classification prefers Groq but degrades automatically to the
  local cheap tier on a provider failure (measured 12-13/14 there) —
  the single-point dependency is closed
## Next steps

1. Revoke + reissue the bot token via BotFather (it passed through a chat
   during setup), and `/setjoingroups` → Disable
2. Review pending memory proposals as they accumulate
   (`python scripts/review_memory.py`) — the manual-review weeks the plan
   calls for start now
3. CI for the test suite
4. Consider whether `anthropic`/`openai` should become the default tiers
   once real budget is allocated (currently free-tier providers only)
5. Phase 2 groundwork: tool registry design, first MCP server (likely
   Home Assistant or a calendar) — not started
