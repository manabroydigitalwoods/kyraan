# Kyraan

Personal multi-agent assistant. This is the Phase 1 skeleton: one Telegram
channel, one orchestrator, two model tiers, MD-file memory with
check-before-write, and a Control Plane that gates everything through a
kill switch, permission config, and DND rules.

- [docs/plan.md](docs/plan.md) — the full vision, architecture, and phase
  roadmap (Phase 0 through Phase 5)
- [docs/progress.md](docs/progress.md) — what's actually been built so
  far, key decisions made, and what's next

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, and whichever
                        # model provider key(s) config/permissions.yaml's tiers use
```

Get `TELEGRAM_OWNER_ID` by messaging [@userinfobot](https://t.me/userinfobot)
on Telegram. Create the bot itself via [@BotFather](https://t.me/BotFather).

## Run

```bash
source .venv/bin/activate
python -m kyraan.main
```

The bot only responds to `TELEGRAM_OWNER_ID` — everyone else is ignored and
logged. Try: "remind me to call the plumber tomorrow at 5pm", "what
reminders do I have", or any general question.

## Model providers

`config/permissions.yaml` has a `providers` registry — every provider
Kyraan knows how to call, each just a `kind` (`anthropic` | `gemini` |
`openai_compatible`) plus connection info. `model_tiers` then picks a
`provider` + `model` per tier:

```yaml
model_tiers:
  cheap:
    provider: ollama
    model: llama3.2
  frontier:
    provider: groq
    model: openai/gpt-oss-120b
```

Currently: `cheap` runs on local Ollama (no key, no rate limit — good for
intent normalization and everyday replies), `frontier` runs on Groq (still
free, but a real ~120B-class model for harder escalations, served fast).
`anthropic`, `gemini`, `openai`, `opencode`, and `openrouter` are all
already configured in the registry and ready to assign to a tier — swapping
either field, or adding a third tier, is a config-only change; no code in
`src/kyraan/model_router/router.py` needs to change. Adding a brand new
`openai_compatible` gateway later (another base_url/key pair) is the same:
add an entry under `providers`, nothing else.

Live-tested findings worth knowing before picking a provider:
- **OpenCode Zen**'s free models share one account-wide rate limit that
  trips fast — fine for a quick check, not for real dev iteration
- **Gemini**'s free tier caps `gemini-3.7-flash` at 20 requests/day — too
  little for anything but the lightest testing
- **Groq** and **OpenRouter**'s free models are "reasoning" models that
  spend tokens on hidden reasoning before the visible answer — give them
  real `max_tokens` headroom (the router defaults to 1024) or the visible
  text comes back empty
- **local Ollama** has no rate limit at all; pull whatever fits your
  hardware (`ollama pull llama3.2`) and reference that tag as the model id

## Test

```bash
pytest -q
```

## Kill switch

```bash
touch KILL_SWITCH   # halts all skill execution and proactive sends immediately
rm KILL_SWITCH       # resumes
```

## Layout

- `src/kyraan/control_plane/` — kernel (permission + kill-switch gate), config
  loader, DND rules, structured event logging (`logs/events.jsonl`)
- `src/kyraan/model_router/` — cheap/frontier tier routing against the
  provider registry in `config/permissions.yaml`
- `src/kyraan/memory/` — Markdown fact store; writes land in
  `memory/pending_review/` for manual approval, never live directly
- `src/kyraan/intent/` — cheap-model typo/slang normalization + confidence
- `src/kyraan/triggers/` — reminder persistence + scheduling (via Telegram's
  JobQueue), DND/kill-switch gated
- `src/kyraan/agents/orchestrator.py` — the single Phase 1 orchestrator
- `src/kyraan/channels/telegram_bot.py` — the one channel
- `config/permissions.yaml` — every skill's permission level (`auto` /
  `confirm`) and model tier; unlisted skills default to `confirm`
- `memory/` — the actual fact files (git-tracked; `pending_review/` is not)

## Reviewing proposed memory writes

Extraction never writes directly to `memory/`. Check
`memory/pending_review/` periodically:

```python
from kyraan.memory import store
from pathlib import Path

for p in sorted(store.PENDING_DIR.glob("*")):
    print(p.read_text())
    # store.promote(p)  # approve
    # store.reject(p)   # discard
```

## Status

See [docs/progress.md](docs/progress.md) for what's built, what's not, and
what's next.
