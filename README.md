# Kyraan

Personal multi-agent assistant. This is the Phase 1 skeleton: one Telegram
channel, one orchestrator, two model tiers, MD-file memory with
check-before-write, and a Control Plane that gates everything through a
kill switch, permission config, and DND rules. See `docs/plan.md` (or your
own copy of the master plan) for the full roadmap — this repo currently
implements Phase 0 + Phase 1 only.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, ANTHROPIC_API_KEY
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
- `src/kyraan/model_router/` — cheap/frontier tier routing via the Anthropic API
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

## Not yet built (see master plan for phasing)

Vector RAG, relationship graph, multi-agent routing, Home/Work agents,
Woodsportal/Home Assistant tools, reflection loop, curiosity queue, cost
monitoring, eval harness, and everything in Phase 3+.
