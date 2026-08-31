# Kyraan

Personal AI assistant, self-hosted on a Mac, talked to over Telegram
(owner-only). Phases 1 and 2 are complete: a model-driven agent loop is
the brain, every tool call runs through a kernel with a kill switch,
per-tool permission levels, and an audit log, and every write needs an
explicit yes in chat.

- [docs/plan.md](docs/plan.md) — the full vision, architecture, and phase
  roadmap (Phase 0 through Phase 5)
- [docs/progress.md](docs/progress.md) — what's actually been built,
  key decisions with their evidence, and what's next
- [docs/design/tool_registry.md](docs/design/tool_registry.md) — how
  tools are declared, gated, and executed

## What it can do

- **Conversation, Q&A, writing, code** — frontier model with owner-reviewed
  memory and rolling conversation context; voice notes transcribed locally
  (Whisper on Apple MLX — audio never leaves the machine)
- **Reminders** — one-shot or recurring (daily/weekdays/weekly/monthly, or
  intervals with a daily window: "every hour from 10am to 9pm remind me to
  drink water"); sub-15-minute intervals show the message-volume math and
  need a yes
- **Google Calendar** — reads via the calendar's secret ICS URL; creates and
  deletes events via OAuth, each write confirm-gated with the concrete event
  named in the ask
- **Email** — unread senders + subjects only, never bodies (deliberate
  privacy boundary, enforced at the Gmail scope *and* kept out of cloud
  prompts)
- **Smart home** — Home Assistant (Tapo): AC plug state, live power/energy,
  bedroom temperature/humidity; switching is confirm-gated; entity allowlist
  in config — unlisted entities don't exist for Kyraan
- **Web search** — self-hosted SearXNG in Docker (free, keyless); snippets
  only, with a deterministic taint rail: once web text enters a turn, every
  non-read tool is locked for the rest of it
- **Weather** — Open-Meteo (free, keyless): current conditions + 3-day
  forecast by place name or shared location pin
- **Nearby places** — hospitals, pharmacies, ATMs, restaurants, hotels,
  sightseeing, fuel, police, groceries around a pin or named place;
  OpenStreetMap by default, Google Places automatically when
  `GOOGLE_MAPS_API_KEY` is set; results carry distances + map links
- **Location pins** — share a Telegram location and it's reverse-geocoded
  (OSM Nominatim) into the conversation; "weather here" / "hospital near me"
  just work
- **Scheduled agent tasks** — "every evening at 8 check tomorrow's calendar
  and warn me about early meetings": an instruction run at a set time with
  read-only tools, results delivered as messages
- **Proactive briefs** — morning (07:30) and evening (21:30): calendar,
  reminders, home status — composed deterministically, no model call, so a
  proactive message can never hallucinate
- **Memory** — facts you state are extracted conservatively, queued for
  your review ("review memory" in chat, or `scripts/review_memory.py`), and
  only then go live; "forget that" is confirm-gated; nothing trains anything
- **Self-accounting** — "how much did we spend this week?" reports its own
  model calls, tokens, and cost; a daily budget cap hard-stops spending

## Architecture in one paragraph

`telegram_bot.py` (the one channel) → `agents/orchestrator.py` →
`agents/agent_loop.py`: a frontier model reads the conversation, memory,
and a tool menu, then decides — call a tool (and see its result) or reply.
Every tool call goes through `control_plane/kernel.py`: kill switch,
permission gate (`auto`/`confirm`), param validation, loop rails (step cap
+ repeat detection), audit log (`logs/events.jsonl`). A confirm-gated
action stashes the exact call; your "yes" replays it byte-identical. If the
cloud is down, the same loop runs on the local model; if that fails too, a
classifier fallback still handles the basics. Tools are declared in
`config/permissions.yaml` and served by adapters in `src/kyraan/tools/`
(builtin or MCP-stdio transport — moving a tool is a config change).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in what you use — each capability lights up
                       # automatically when its env var is present
```

Get `TELEGRAM_OWNER_ID` by messaging [@userinfobot](https://t.me/userinfobot);
create the bot via [@BotFather](https://t.me/BotFather). The capability
brief the model sees is generated from config + environment, so a missing
credential means Kyraan honestly says that feature isn't connected — nothing
else breaks.

### Supporting containers (Docker)

All container config lives in [docker/](docker/) — see the comments in
[docker-compose.yml](docker/docker-compose.yml):

```bash
cd docker
cp .env.example .env    # SearXNG secret (openssl rand -hex 32)
docker compose up -d    # SearXNG (web search) + Home Assistant
```

Home Assistant's `/config` under `docker/homeassistant` is mostly runtime
state — only its declarative YAML is git-tracked. **Never run
`git clean -fdx` in this repo**; it would delete that untracked state.

## Run

Development, in the foreground:

```bash
python -m kyraan.main
```

Production, as a launchd agent (starts at login, restarts on crash, a
watchdog sweeps every 5 minutes; plists in `~/Library/LaunchAgents/`):

```bash
launchctl kickstart -k gui/$(id -u)/ai.kyraan   # restart after code/.env changes
```

Logs: `logs/bot.log` (process), `logs/events.jsonl` (the audit trail —
every model call, tool call, and gate decision; rotated, never deleted).

## Model tiers

`config/permissions.yaml` declares a provider registry and two tiers.
Currently: **cheap → local Ollama `qwen3:8b`** (degraded-mode brain and
extraction), **frontier → OpenAI `gpt-5.4-nano`** (the agent loop —
benchmarked head-to-head against alternatives on the production prompts;
~$0.10/day at real usage, hard daily budget cap at $5). Anthropic, Gemini,
Groq, OpenRouter, and OpenCode are configured and one config edit away.
Cost notes and free-tier landmines are documented inline in the YAML.

## Dev tools

`scripts/panel.py` is the web panel — read-only mission control over what
Kyraan already logs. The overview deck shows six consoles at once
(systems, budget with a 7-day sparkline, schedule with overdue jobs, the
live event tail, top turns by tokens, and the 24h anomaly census); five
more sectors open the same data full-screen, including per-turn forensics
with every model call, tool call and timing, and a brain view that draws the
whole second brain as one graph — memories, the people they are about,
queued work, and skills — wired by embedding similarity, stored triples,
and which tools actually fire in the same turn, with live neuron pulses
off the event stream. Every sector is a
real URL, so a reload or a shared link lands on the same view with the
same filters. It binds 127.0.0.1 and
prints a URL carrying a one-time token; reach it from a phone over
Tailscale, never a forwarded port. It writes nothing — see
[docs/design/web_panel.md](docs/design/web_panel.md) for why that matters
and what Phases B-D would add. The look is a CRT phosphor terminal: amber,
green, or P1 blue, switched from the header and remembered per browser.

```bash
python scripts/panel.py                      # http://127.0.0.1:8765
KYRAAN_PANEL_TOKEN=... python scripts/panel.py   # a URL that survives restarts
```

`scripts/chat.py` (CLI) and `scripts/tui.py` (full-screen Textual
dashboard) exercise the real orchestrator without Telegram. The TUI shows
per-turn provider/model/latency/tokens, cost vs. budget, collapsible
reasoning, `/tier` runtime overrides, and `/export`.

## Test

```bash
pytest -q   # 370+ tests; production data stores are isolated by fixture
```

## Kill switch

```bash
touch KILL_SWITCH   # halts all skill execution and proactive sends immediately
rm KILL_SWITCH      # resumes
```

Re-checked before every action, including at confirmation time — a "yes"
after the switch is engaged does nothing.

## Layout

- `src/kyraan/control_plane/` — kernel (permission + kill-switch gate + loop
  rails), config, DND rules, event logging
- `src/kyraan/agents/` — orchestrator, the agent loop (primary brain), the
  generated capability brief, deterministic guards
- `src/kyraan/tools/` — registry + adapters: google_calendar, gmail,
  home_assistant, web_search, weather, places
- `src/kyraan/model_router/` — tier routing, retries, cost ledger, usage
  reports
- `src/kyraan/memory/` — Markdown fact store + engine (ranking, supersession,
  discretion flags); writes queue in `memory/pending_review/` for approval
- `src/kyraan/triggers/` — reminders, briefs, scheduled agent tasks, home
  alerts, nightly self-review
- `src/kyraan/channels/` — telegram_bot, voice (local Whisper), location
  (pin reverse-geocoding)
- `config/permissions.yaml` — every skill and tool with its permission
  level; write tools are confirm-gated by a validator that refuses to load
  anything else
- `src/kyraan/panel/` — the read-only web panel (Phase A): queries over
  the logs and stores, a stdlib HTTP server, and a page that builds every
  node with textContent
- `docker/` — compose file + container configs (SearXNG, Home Assistant)
