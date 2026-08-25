# Progress

Tracks what's actually been built against [plan.md](plan.md), so status
lives in the repo instead of scattered across chat history. Update this
when a phase milestone lands or a load-bearing decision gets made —
day-to-day commits speak for themselves in `git log`.

## Where we are

**Phase 0 (Foundations) and Phase 1 (Core Brain)** are built and working,
started and iterated on 2026-08-25 — including the first real end-to-end
Telegram session the same day. What remains inside Phase 1's spirit is the
memory loop (see Known limitations). Phases 2–5 are not started.

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
  the specific model); `router.session_cost_usd` accumulates spend,
  checked against `cost_monitor.daily_budget_usd` in the TUI sidebar
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

**Tests**: 48 passing (`pytest -q`) — kernel gating, DND wraparound, memory
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

- **The memory subsystem is unwired**: `memory/store.py` (propose/promote/
  reject) is built and tested, but nothing calls `propose_fact` — there is
  no extraction pass in the orchestrator, nothing reads memory into
  qa.answer's prompt, and the `memory/` tree is empty (Phase 0's manual
  seeding was never done). Phase 1's "conservative extraction" item is
  therefore not actually complete — the store exists, the pipeline doesn't
- **No conversation context**: every message is handled statelessly;
  follow-up questions ("what about tomorrow?") have nothing to refer to
- `cancel_reminder`'s cancel path is best-effort in both dev harnesses (an
  already-scheduled asyncio task still fires even if the record is
  cancelled) — fine for dev, not for production
- The confirm flow's pending state is in-memory only — a restart drops an
  unanswered confirmation (fails safe: the action just doesn't run)
- No CI (tests only run when run by hand) and no rotation for
  `logs/events.jsonl` (~180 KB after one dev day — and it's the audit
  trail, so it shouldn't be cleaned up ad hoc)
- Cost tracking (`router.session_cost_usd` vs. `cost_monitor.
  daily_budget_usd`) is process-lifetime only — not persisted across
  restarts, no alerting/hard-stop at the budget yet, just a visible number
  in the TUI sidebar
- Section 3a governance gaps (family consent, work/personal data boundary,
  third-party data exposure policy) are **unresolved** and block Phase 3 —
  nothing here should be rolled out to family members yet
- No RAG, relationship graph, agent router, tool registry, reflection loop,
  or curiosity queue — all Phase 2+, not started
- Intent classification depends on Groq specifically, with no automatic
  local fallback wired in — if it degrades or rate-limits under real
  (non-dev-loop) usage, classification breaks even though qa.answer and
  reminders.create would keep working locally. Would need to be added
  deliberately, informed by what actually breaks first
- JSON parsing (intent classification, reminder extraction) doesn't strip
  a markdown code fence if the model wraps its JSON in one — seen live
  from llama3.1:8b once; the intent was actually correct, just unparsed

## Next steps

1. Revoke + reissue the bot token via BotFather (it passed through a chat
   during setup), and `/setjoingroups` → Disable; also decide how the bot
   should run long-term (launchd/systemd service vs. manual start)
2. **Wire the memory loop** — the missing half of Phase 1: extraction pass
   after `handle_message` calling the existing `propose_fact()`, memory
   reads feeding qa.answer's prompt, seed the empty `memory/` tree, and a
   short rolling conversation history in the same change
3. CI for the test suite + rotation for `logs/events.jsonl`
4. Consider whether `anthropic`/`openai` should become the default tiers
   once real budget is allocated (currently free-tier providers only)
5. Strip a markdown code fence before parsing model JSON output (intent
   classification, reminder extraction) — small, low-risk robustness fix
6. Phase 2 groundwork: tool registry design, first MCP server (likely
   Home Assistant or a calendar) — not started
