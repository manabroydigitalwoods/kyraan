# Kyraan Tool Inventory

Live snapshot as of 2026-08-28. Every tool listed here runs through
`kernel.run_tool`/`kernel.run_skill` — kill switch, permission gates,
loop rails, audit log — regardless of which adapter backs it. `(confirm)`
marks a write that stops for the owner's explicit yes before executing.

See `docs/design/tool_registry.md` for the original design rationale.

## External adapters

Configured in `config/permissions.yaml` under `tool_servers`. Each is a
`transport: builtin` Python module in `src/kyraan/tools/`.

### calendar — `google_calendar.py`
Credential: `GOOGLE_CALENDAR_ICS_URL` (read) + Google OAuth (write). **Live.**

| Tool | About |
|---|---|
| `calendar.list_events` | Events in a time window (id/title/start/recurring) |
| `calendar.create_event` | Create an event `(confirm)` — a past start date is refused |
| `calendar.delete_event` | Delete one event by id `(confirm)` — warns if the event is recurring (deletes the whole series) |
| `calendar.reschedule` | Move an existing event to a new time |

### gmail — `gmail.py`
Credential: Google OAuth. **Live.** Bodies are **local-only**
(`KYRAAN_EMAIL_BODIES=local` — summarized on-device, never sent to a
cloud model). Drafts are **on** (`KYRAAN_EMAIL_DRAFTS=on`).

| Tool | About |
|---|---|
| `email.unread` | Unread senders and subjects (metadata only unless local bodies are on) |
| `email.read` | Read and summarize unread bodies — processed entirely on-device |
| `email.search` | Filter mail by sender/subject words and/or a Gmail query |
| `email.important` | A priority digest of unread mail (VIP senders / important keywords) |
| `email.mark_read` | Mark one unread email read |
| `email.archive` | Archive one email out of the inbox (reversible) |
| `email.draft` | Save a Gmail draft — never sends; the owner sends from Gmail |

### web — `web_search.py`
Credential: `SEARXNG_URL` (self-hosted SearXNG). **Live.**

| Tool | About |
|---|---|
| `web.search` | Live web search — titles, URLs, snippets only; cannot open pages |

### weather — `weather.py`
Credential: none (Open-Meteo, keyless). **Live.**

| Tool | About |
|---|---|
| `weather.get` | Live weather + 3-day forecast for a named place or shared pin |

### places — `places.py`
Credential: `GOOGLE_MAPS_API_KEY`. **Live.**

| Tool | About |
|---|---|
| `places.nearby` | Nearby places by category, with distance + map links |

### routes — `routes.py`
Credential: `GOOGLE_MAPS_API_KEY`, `TOMTOM_API_KEY` fallback. **Live.**

| Tool | About |
|---|---|
| `routes.eta` | Distance and travel time with live traffic |

### home_assistant — `home_assistant.py`
Credential: `HASS_URL` + `HASS_TOKEN`. **Live.** Allowlisted to the
bedroom AC plug only — an entity not named in config doesn't exist to
Kyraan.

| Tool | About |
|---|---|
| `home.get_state` | Read a smart-home entity's state |
| `home.turn_on` | Switch a plug ON `(confirm)` |
| `home.turn_off` | Switch a plug OFF `(confirm)` |

## Local-only tool groups

No external adapter — these read/write Kyraan's own on-disk stores
(JSON files + Postgres mirror) and never leave the machine.

### reminders
| Tool | About |
|---|---|
| `reminders.create` | Set a reminder — one-shot or recurring (daily/weekdays/weekly/monthly/interval with a daily window); min interval 5 min, under 15 min asks the owner to confirm message volume |
| `reminders.list` | The owner's pending reminders |
| `reminders.snooze` | Push a reminder back N minutes |
| `reminders.reschedule` | Move a pending reminder to a new time in place |
| `reminders.cancel` | Cancel one pending reminder by id |

### tasks (scheduled agent runs)
| Tool | About |
|---|---|
| `tasks.schedule` | Schedule an instruction the assistant runs at a set time with read-only tools `(confirm)` |
| `tasks.list` | The owner's scheduled agent tasks |
| `tasks.cancel` | Cancel a scheduled agent task by id |

### rules (standing watch conditions)
| Tool | About |
|---|---|
| `rules.create` | Create a watch rule — a standing condition checked periodically |
| `rules.list` | The owner's active watch rules |
| `rules.cancel` | Remove a watch rule by id |

### memory
| Tool | About |
|---|---|
| `memory.forget` | Forget a saved fact `(confirm)` — deactivates, kept as history |
| `memory.pending_list` | Facts queued for the owner's review |
| `memory.recall_episodes` | Search past conversations beyond recent history |
| `memory.relations` | Relations from the saved-fact graph, with source facts |

### documents
| Tool | About |
|---|---|
| `documents.list` | The owner's saved documents (caption, kind, date) |
| `documents.read` | Read a saved document in full (clipped ~6000 chars) |
| `documents.search` | Search saved documents (text from photos/files) |
| `documents.rename` | Rename a saved document |
| `documents.show` | Send the user the original uploaded file |

### faces (on-device recognition — templates never leave the machine)
| Tool | About |
|---|---|
| `faces.remember` | Save the face from the most recent photo (within ~10 min) |
| `faces.check_photo` | Re-run face recognition on a photo already in the conversation |
| `faces.list` | Which faces are actually enrolled for recognition |

### persons (multi-person access — owner-only for writes)
| Tool | About |
|---|---|
| `persons.list` | Every person Kyraan tracks — ids, other names, face status |
| `persons.profile` | Everything known about one person in one call |
| `persons.add` | Add a new friend/contact to the person registry |
| `persons.alias` | Give an existing person another name |
| `persons.set_access` | Owner only: grant/revoke a person's chat access |
| `persons.set_tools` | Owner only: grant/revoke specific abilities for a person |

### other
| Tool | About |
|---|---|
| `files.send` | Send the user a real file — the original upload or one composed by Kyraan |
| `usage.report` | Kyraan's own AI usage — calls, tokens, cost, budget picture |
| `my.abilities` | What the current speaker can do here — their access level |

## Summary

- **7 external adapters**, all credentialed and live: calendar, gmail,
  web, weather, places, routes, home_assistant
- **10 local-only tool groups**: reminders, tasks, rules, memory,
  documents, faces, persons, files, usage, my.abilities
- **47 tools total**
