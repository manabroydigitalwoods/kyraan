# Progress

Tracks what's actually been built against [plan.md](plan.md), so status
lives in the repo instead of scattered across chat history. Update this
when a phase milestone lands or a load-bearing decision gets made —
day-to-day commits speak for themselves in `git log`.

## Where we are

**Phase 0 (Foundations) and Phase 1 (Core Brain)** are built and working,
started and iterated on 2026-08-25. Phases 2–5 are not started.

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
  provider+model; currently **cheap → local Ollama (`llama3.2`)**,
  **frontier → Groq (`openai/gpt-oss-120b`)**
- Retry-with-backoff on transient errors (rate limits, 5xx) — every cloud
  provider tested has hit these live
- Captures latency, token usage, reasoning/"thinking" text, and cost
  (`RoutedResponse`) per call — provider SDKs disagree on field names, so
  any of these can come back `None`/`0` rather than raising
- Cost tracking: a tier can declare `pricing` (USD per 1M tokens, tied to
  the specific model); `router.session_cost_usd` accumulates spend,
  checked against `cost_monitor.daily_budget_usd` in the TUI sidebar
- Runtime tier overrides (`config.set_tier_override()`) — repoint a tier at
  a different provider/model for the rest of the process without editing
  `permissions.yaml` or restarting (exposed via the TUI's `/tier` command)
- `call_with_escalation()`: cheap tier first, frontier on failure

**Intent Normalization** (`src/kyraan/intent/`)
- Cheap-tier classification into `reminders.create/list/cancel`,
  `qa.answer`, or `unknown`, with a confidence score
- Escalates to the frontier tier once before giving up, when the cheap
  tier (a small local model) is genuinely unsure
- Hardened against malformed model output: JSON `null` fields, intent
  strings outside the known set — both used to crash or misroute

**Memory** (`src/kyraan/memory/`)
- MD files only (no RAG/graph yet, per plan)
- Check-before-write: extraction proposes into `memory/pending_review/`,
  a human promotes or rejects — nothing reaches live memory unreviewed

**Triggers** (`src/kyraan/triggers/`)
- Reminders are the only proactive trigger so far
- Persisted to `data/reminders.json`, scheduled via the channel's own
  event loop (Telegram's JobQueue, or a Textual-app-safe equivalent in the
  dev tools) — gated by DND + kill switch on every fire

**Orchestrator & Channel**
- `src/kyraan/agents/orchestrator.py` — the single Phase 1 orchestrator
  (no agent router yet, that's Phase 3)
- `src/kyraan/channels/telegram_bot.py` — the one channel, owner-only

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

**Tests**: 39 passing (`pytest -q`) — kernel gating, DND wraparound, memory
propose/promote/reject, scheduler timezone handling, router provider
dispatch, intent normalization against malformed output, cost-calculation
math, orchestrator error handling, and headless Textual Pilot tests for the
TUI (including retry, tier override, and transcript export).

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

## Key decisions made

- **Stack**: Python, self-hosted (not cloud VM) — see `pyproject.toml`
- **Model providers**: local Ollama + Groq as the working default, after
  live-testing ruled out OpenCode Zen (account-wide rate limit trips fast)
  and Gemini's free tier (hard 20 requests/day cap on `gemini-3.7-flash`)
  as unworkable for real iteration
- Real API keys for Gemini, OpenAI, OpenCode, Groq, and OpenRouter are in
  the local `.env` (gitignored, never committed) — Anthropic's is not yet
  obtained. Swapping a tier's provider is a one-line config edit, or a
  `/tier` command in the TUI for a temporary session-only switch

## Known limitations / not yet done

- **Telegram bot token + owner ID**: still not set — the real bot has
  never been run end-to-end, only the CLI/TUI dev harnesses (which use the
  identical orchestrator code path)
- `cancel_reminder`'s cancel path is best-effort in both dev harnesses (an
  already-scheduled asyncio task still fires even if the record is
  cancelled) — fine for dev, not for production
- Cost tracking (`router.session_cost_usd` vs. `cost_monitor.
  daily_budget_usd`) is process-lifetime only — not persisted across
  restarts, no alerting/hard-stop at the budget yet, just a visible number
  in the TUI sidebar
- Section 3a governance gaps (family consent, work/personal data boundary,
  third-party data exposure policy) are **unresolved** and block Phase 3 —
  nothing here should be rolled out to family members yet
- No RAG, relationship graph, agent router, tool registry, reflection loop,
  or curiosity queue — all Phase 2+, not started

## Next steps

1. Get `TELEGRAM_BOT_TOKEN` + `TELEGRAM_OWNER_ID`, run the real bot
   end-to-end for the first time
2. Consider whether `anthropic`/`openai` should become the default tiers
   once real budget is allocated (currently free-tier providers only)
3. Phase 2 groundwork: tool registry design, first MCP server (likely
   Home Assistant or a calendar) — not started
