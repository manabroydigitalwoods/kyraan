# Kyraan — Tools, Skills, Capabilities & Connectors

As-built reference, 2026-08-30. The ground truth is `config/permissions.yaml`
(tool registry, stage toolsets, persona) and `src/kyraan/agents/loop_tools.py`
(executors + menu); this doc is the human-readable map of both.

---

## 1. The brain

- **Frontier tier** — OpenAI `gpt-5.4-nano` (paid, cloud). Handles reply
  decisions, tool orchestration, extraction.
- **Cheap tier** — local `qwen3:8b` via Ollama. Automatic fallback when the
  frontier is down or out of credits; slower, honest about being degraded.
- **Embeddings** — local `all-minilm` (384-d) via Ollama. Never leaves the machine.
- **Voice** — Telegram voice notes transcribed locally (Whisper). Audio never
  leaves the machine.
- Config lists more providers ready to swap in (Anthropic, Gemini, Groq,
  OpenRouter, OpenCode) — one YAML edit each, none active today.

---

## 2. Conversational tools (the loop's menu)

Every tool is declared in `permissions.yaml` with permission (`auto` /
`confirm`), side-effect class, retries, and timeout. Writes are confirm-gated;
scheduled runs get read-only tools by construction.

### Reminders & scheduling
- `reminders.create` / `list` / `cancel` / `reschedule` / `snooze` / `recreate` —
  one-shot or recurring (daily / weekdays / weekly / monthly / intervals with a
  daily window). Sub-15-minute intervals show the pings-per-day math and need a yes.
- `tasks.schedule` / `list` / `cancel` / `recreate` — **agent tasks**: an
  instruction that RUNS at a set time with read-only tools ("every evening at 8,
  check tomorrow's calendar and warn me about early meetings").
- `goals.create` / `list` / `show` / `update` / `set_status` — **goals**:
  pursuits that survive across days ("plan Kiaan's birthday") with
  steps, a findings journal, and a daily read-only research cycle that
  pings only on real progress. Max 3 active per person; cycles run at
  the goal owner's own access level.
- `rules.create` / `list` / `cancel` / `reactivate` — **watch rules** on home
  entities ("if the AC is on more than 3 hours, tell me"). Edge-triggered: one
  alert per crossing, never a nag. Notify-only by doctrine — a rule can check
  and tell, never act.

### Calendar
- `calendar.list_events` / `get_event` — read via the calendar's secret ICS
  address (no OAuth needed for reads).
- `calendar.create_event` / `update_event` / `delete_event` / `reschedule` —
  writes via Google OAuth, each behind a confirm.

### Email (Gmail)
- `email.unread` / `email.read` — metadata always; **bodies are fetched
  local-only** and never enter cloud prompts.
- `email.important` — deterministic digest: Gmail's IMPORTANT label ∪ owner's
  VIP senders ∪ keyword list (both in `permissions.yaml` `email:` block), with
  the reason named per mail.
- `email.search` — label-scoped search.
- `email.draft` / `draft_delete` — compose drafts in Gmail. **There is no send
  path anywhere in the codebase** — sending stays with the owner.
- `email.mark_read` / `mark_unread` / `archive` / `unarchive` — opt-in
  (`KYRAAN_EMAIL_MODIFY=on`), confirm-gated, undoable.

### Memory & knowledge
- Fact extraction from conversation → **owner-reviewed pending queue** → only
  reviewed facts go live. Identity claims never become facts (registry territory).
- `memory.recall_episodes` — same-day episodic recall (kept within ~30 min of live).
- `memory.relations` — the person/fact/document graph (triples).
- `memory.forget` / `unforget` / `pending_list` — forget is undoable.
- Nightly memory-dedup scan proposes consolidations; owner applies.
- **Learned rules** — corrections you repeat become proposed behavior
  rules (drafted locally, owner-approved into the persona, capped,
  retirable via "retire learned rule …").

### Documents & files
- Uploaded files (photos of cards, PDFs, etc.) are OCR'd/read, titled cleanly,
  linked to the people they're about (multi-person supported), byte-hash
  deduped, and the **originals stored locally** (`data/documents/`, 0600).
- `documents.list` / `search` / `read` / `rename` / `show` — show sends the
  original back (images inline).
- `files.send` — send a stored text-format file to the requester's own chat.

### People & identity
- `persons.add` / `alias` / `list` / `profile` — the person registry; aliases
  make one deterministic name→person resolver joining faces, documents, and memories.
- `faces.remember` / `check_photo` / `list` / `forget` — face templates stored
  locally, keyed by person id. **Biometrics are owner-governed**: every face
  path requires the owner's own turn, regardless of grants.
- Who-is-speaking comes from the registry (SPEAKER header), never from
  extracted facts — fail-closed: an unidentified viewer is never the owner.

### Access control (owner authority)
- `persons.set_access` — grant/revoke a person's stage (chat ability). Grants
  need consent + a linked chat; demotion is ungated; undoable.
- `persons.set_tools` — per-person extra tool grants beyond their stage.
- `my.abilities` — anyone can ask what they're allowed to do; the answer is
  viewer-aware.
- Media capabilities (`media.photo` / `file` / `voice` / `location`) gate what
  kinds of messages a person may even send in.
- Authority tools themselves are never grantable.

### World information
- `web.search` — self-hosted SearXNG; snippets only. Results are tainted: any
  turn that saw web text has all non-read tools blocked for the rest of the turn.
- `weather.get` — Open-Meteo, exact current + 3-day forecast, by name or pin.
- `places.nearby` — "hospital near me", by shared pin or named place.
- `routes.eta` — distance + duration with live traffic (Google Routes primary,
  TomTom fallback).

### Music (Spotify)
- `music.play` / `pause` / `volume` / `devices` — playback on any of the
  owner's Spotify Connect devices, **Echos included** (no Alexa API
  needed). Play/pause are the ONE named exemption from the
  every-write-confirms rule (owner decision 2026-09-02 — audible
  actions verify themselves); volume confirms above 70% (40% in quiet
  hours); receipts re-read the player state. Owner-only.

### Home control
- `home.get_state` / `turn_on` / `turn_off`, `switch.ac` — Home Assistant, via
  an explicit entity allowlist (currently: the bedroom AC plug + its sensors).
  An entity not in the allowlist does not exist for Kyraan. Writes confirm-gated.

### Self-knowledge
- `usage.report` — AI spend by day vs budget.
- `system.status` / `vm.swapusage` — host health.

---

## 3. Proactive behaviors (standing jobs)

All proactive sends pass the kill switch + DND (quiet hours) and are
delivery-truthful: nothing is marked sent unless it actually landed; misses
fire late rather than never (sleep-proof since 2026-08-30).

- **Morning & evening briefs** — calendar, reminders, home readings, energy
  use, important-mail line, one curiosity question per day (14-day re-ask spacing).
- **Reminders / agent tasks / watch rules** — as scheduled above.
- **Home alerts** — 30-min poll (e.g. the AC-running-for-hours heads-up).
- **Nightly self-review** — reviews the day's exchanges, reports what looked
  wrong; corrections feed the eval suite.
- **Nightly health check** — Postgres, Redis, Ollama models, embedder,
  SearXNG, OpenAI key; WARN/CRITICAL to the owner. Credit exhaustion raises a
  CRITICAL immediately.
- **Nightly maintenance** — document orphan sweep, memory dedup scan, backups
  (0600 dumps), same-day episode catch-up every 30 min.

---

## 4. Adapters / connectors

| Connector | What | Auth | Data boundary |
|---|---|---|---|
| Local CLI | second channel: `python -m kyraan.channels.cli` — same brain/memory, conversation-only (no proactive jobs) | terminal access | fully local |
| Telegram Bot API | The primary chat channel (text, voice, photos, files, pins) | bot token (.env) | messages in/out |
| OpenAI | frontier model | API key | prompts exclude local-only data |
| Ollama (local) | cheap model + embeddings + transcription | none (localhost) | fully local |
| Gmail API | unread/read/important/search/drafts/modify | OAuth (readonly+metadata, compose & modify opt-in) | bodies local-only |
| Google Calendar | reads via secret ICS; writes via OAuth | ICS URL / OAuth | — |
| Home Assistant (local) | entity reads/writes | long-lived token | allowlisted entities only |
| SearXNG (local Docker) | web search | none | only the query leaves the machine |
| Open-Meteo | weather | none | coordinates only |
| Google Routes / TomTom | live-traffic ETAs | API keys | origin/destination only |
| Any MCP stdio server | mountable via config (none mounted yet) | per-server env | behind our registry: confirm-gated writes, optional untrusted taint, owner-only until granted |
| Postgres (Docker) | facts, persons, documents, episodes, triples, mirrors | local | fully local |
| Redis (Docker) | working state / KV | local | fully local |

---

## 5. Standing guarantees

- Email bodies, biometric templates, and voice audio **never leave the machine**.
- `local_only` facts and unreviewed pending facts never enter cloud prompts.
- No email sending, no acting watch rules, no browser automation — each is a
  deliberate governance hold, not a gap.
- Every write tool is confirm-gated and has an undo mapping.
- Non-owner tool reach is frozen in tests; changing it requires editing both
  config and test.
