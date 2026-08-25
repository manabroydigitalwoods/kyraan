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
- Captures latency, token usage, and reasoning/"thinking" text per call
  (`RoutedResponse`) — provider SDKs disagree on field names, so any of
  these can come back `None` rather than raising
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
- `scripts/tui.py` — a full-screen Textual dashboard: session stats,
  per-turn provider/model/latency/token display, a thinking spinner, and
  collapsible "Thought" sections showing a reasoning model's hidden
  chain-of-thought (matching OpenCode's UI pattern)

**Tests**: 21 passing (`pytest -q`) — kernel gating, DND wraparound, memory
propose/promote/reject, scheduler timezone handling, router provider
dispatch, intent normalization against malformed output, and headless
Textual Pilot tests for the TUI.

## Key decisions made

- **Stack**: Python, self-hosted (not cloud VM) — see `pyproject.toml`
- **Model providers**: local Ollama + Groq as the working default, after
  live-testing ruled out OpenCode Zen (account-wide rate limit trips fast)
  and Gemini's free tier (hard 20 requests/day cap on `gemini-3.7-flash`)
  as unworkable for real iteration
- Real API keys for Anthropic *(not yet obtained)*, Gemini, OpenAI,
  OpenCode, Groq, and OpenRouter are in the local `.env` (gitignored,
  never committed) — swapping a tier's provider is a one-line config edit

## Known limitations / not yet done

- **Telegram bot token + owner ID**: still not set — the real bot has
  never been run end-to-end, only the CLI/TUI dev harnesses (which use the
  identical orchestrator code path)
- `cancel_reminder`'s cancel path is best-effort in both dev harnesses (an
  already-scheduled asyncio task still fires even if the record is
  cancelled) — fine for dev, not for production
- No cost tracking against `cost_monitor.daily_budget_usd` in
  `permissions.yaml` yet — token usage is captured per-call but not summed
  against a budget or alerted on
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
