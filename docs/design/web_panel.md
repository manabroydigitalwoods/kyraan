# Web Panel — design proposal (drafted 2026-08-31, owner NOT committed)

Status: **Phase A BUILT (2026-08-31)** — `src/kyraan/panel/`, run with
`scripts/panel.py`, 22 tests in `tests/test_panel.py`. Phases B-D remain
proposals and still compete with the §3d build order (web page reading,
goal continuity, MCP mounts, wake scheduling, second channel) — see "The
trade" at the end.

## The question

Does Kyraan need a web panel: memory view with a clustering map, jobs,
workflows, tasks, all configuration, system monitoring, tools and
abilities, MCP and adapter management, files, observability, health,
token consumption and the turns that spend it, a dashboard, a Jarvis
interface?

## Position

Yes — for two of those, now; for the rest, later or never.

| Surface | Verdict |
|---|---|
| Observability: events, health, tokens, cost, top-spending turns, trigger board | **Build.** Cheap, read-only, no governance surface. |
| Memory workbench: review queue, conflicts, subjects, cluster map | **Build.** It unblocks a real gate. |
| Config, permissions, MCP + adapter management | **Build late**, behind the capabilities it manages. |
| Jarvis chat interface | **Don't.** Telegram is already the channel; a second chat UI is a second surface for zero new capability. |

## Why the first two earn it

**The instrumentation already exists.** `logs/events.jsonl` records every
model call, tool call, and gate decision. `data/cost_ledger.json` holds
spend against the daily cap. `scripts/trace.py` already reconstructs a
turn. `control_plane/health.py` already computes health. Postgres +
pgvector hold facts, triples, episodes, persons. **The panel is a reader
of what we already write, not new instrumentation** — which is what makes
a read-only v1 small.

**The memory queue is the actual bottleneck.** progress.md's family
stage-2 gate needs 200 facts reviewed at ≥90% trailing approval. That
review runs one card at a time through a Telegram thread or
`scripts/review_memory.py`. Batch triage, the conflict pair side by side,
a proposal shown against the fact it would supersede — this is the one
job where a screen genuinely beats the chat channel. The panel is not
just a view of progress toward that gate; it is how we get through it.

**The cluster map has a job.** `store/embed.py` + pgvector already give
us fact vectors. A 2D projection colored by subject and sized by recall
count is a duplicate-and-drift finder: tight clusters are redundant
facts, and a pending proposal landing inside an existing cluster is
probably a restatement of it. Built for that job it is useful; built as a
landing-page animation it is decoration. Phase B, driven by dedupe.

## The two rules that make it safe

### 1. The panel is a client of the kernel, never a path around it

Every write endpoint constructs a tool call and hands it to
`control_plane/kernel.py`: same permission level, same kill-switch
re-check, same confirm stash, same audit line in `events.jsonl`. No
panel-owned `UPDATE` statements, ever.

The failure mode this prevents: enforcement living in two places, one of
them newer. A read-only Phase A makes the rule free to hold; it must be
written down before Phase C makes breaking it convenient.

### 2. Never render untrusted text as HTML

The panel will display web-search snippets, MCP server output, email
subject lines, and facts extracted from chat — all attacker-reachable
text, on a machine holding OAuth tokens, face embeddings, and a HASS
token. An XSS in the admin panel is worse than any tool exploit we have
gated so far, because the panel is trusted to drive the kernel.

`taint.py` already names this input class on the way in; the panel needs
its display-side counterpart. Escape everything, no `innerHTML`, strict
CSP, and tainted values rendered with a visible provenance marker.

### Exposure

Bind `127.0.0.1` only; reach it from the phone over Tailscale, never a
forwarded port. One owner token in front of it. The kill switch applies
to the panel exactly as to any channel: engaged means the control
surfaces are inert, not merely hidden.

Localhost + read-only together are what let Phase A skip a governance
round. **The moment writes land (Phase C), it needs one** — a data
destination row like any other capability.

## Phases

**A — read-only glass. BUILT.** Live event stream, filterable by tool / gate
decision / tier. Turn forensics: click a turn, get its model calls, tool
calls, latency, and token split (`trace.py` behind an endpoint). Cost vs.
daily cap, with top-N turns by tokens. Health. And the trigger board —
reminders, agent tasks, briefs, with next-fire times and misfires. That
board alone answers a question we cannot ask today: the vaccination
reminder the MacBook slept through (§3d #4) would have shown as an
overdue row.

What Phase A actually shipped, against the plan above: all of it, plus
two things the build surfaced. Turn rows sort by tokens, cost, or latency
— "which turns spend the budget" was the headline question and it needed
the whole window scored before the cut, not just the recent page. And the
trigger board treats an unparseable schedule as its own state rather than
as "not overdue", because a reminder whose time we cannot read is a
different failure from one that is merely late.

**The shape is a mission-control deck, not a tabbed report** (owner's
call, 2026-08-31). Sector 00 — Overview — is the default and shows six
consoles at once: systems (a lamp per subsystem), budget (figure, gauge,
7-day sparkline), schedule (next five jobs, overdue in red), activity
(the live tail), top consumers (turns by tokens), and the 24h anomaly
census with magnitude bars. Sectors 01-05 are the same data full-screen
when a console raises a question worth chasing.

The premise a command centre rests on: **you do not navigate to find out
that something is wrong.** Three consequences the build had to honour —

- **Every console fails alone.** A dead searxng probe blanks the systems
  console and nothing else; each panel renders its own "unavailable" and
  its own error verdict. A deck where one bad fetch empties the screen is
  worse than tabs.
- **One SSE connection feeds both** the activity console and sector 01,
  so the deck costs no extra stream.
- **Consoles are entry points, not dead ends.** A row in top consumers
  jumps straight to sector 02 with that turn's forensics already open —
  noticing an expensive turn is only useful if looking at it is one click.

Cadence: the status rail polls every 10s (cheap), the deck's slower
consoles every 60s, and only while sector 00 is showing — schedule and
spend move in minutes, and re-probing systems is expensive.

**The look is a CRT phosphor terminal** (owner's call, 2026-08-31, while
the UI was still one page — a reskin is cheap now and expensive once
Phases B-D add surfaces). Amber by default, green and P1 blue switchable
from the header and remembered per browser. Tasteful rather than
full-commitment: palette, double-line rules, uppercase chrome and a
blinking cursor, but no scanline overlay, no flicker, no curvature — this
panel gets stared at for long stretches.

Two rules keep it legible, and both are worth holding as the panel grows:

- **Glow is for chrome, never for body text.** Headings, tabs, chips and
  the budget bar carry a text-shadow; a 200-row event stream does not. A
  glowing log is a blurry log.
- **Chrome uppercases; data does not.** Event kinds are a label column and
  read as uppercase. Turn ids, tool names and repeat strings keep their
  real case — they are what you retype into a grep. (Caught twice in
  review: once on the tags, once on the id in the forensics header.)
- **The no-innerHTML rule covers SVG.** The sparkline is assembled with
  `createElementNS`, because an `<svg>` built from a string is the easiest
  place to forget rule 2.

Dark-only, deliberately: there is no light-mode CRT, and a phosphor
palette inverted onto paper is neither retro nor readable. One owner, one
machine, so it commits.

**The shell is a HUD** (design pass, 2026-08-31). Consoles carry corner
brackets and a notched top-right corner — drawn as gradient bars and a
clip-path, so no extra markup and the tube colours them. Each console
wears its sector index in the frame, headers sit on a rule that fades out
rather than stopping dead, and the scrollbars are in the palette (an
OS-grey bar inside a phosphor frame was the one piece of chrome that gave
away that this is a web page).

Three layout faults the pass fixed, all of them the same mistake — space
spent on nothing:

- The deck stretched every console to its row's tallest, so the systems
  strip padded out a quarter of the screen holding one line. Rows are
  content-sized now (`align-items: start`), with only the last row taking
  the slack so the deck reaches the footer.
- Anomalies sat in a half-empty row leaving two dead columns. Regrouped:
  a full-width lamp strip, then the three numbers you check first, then
  the two long lists side by side and equal.
- Console titles centred themselves, because `space-between` was spreading
  index, name and verdict evenly. The index and name belong together.

**One shell for every sector.** The overview was a HUD and the other five
views were bare divs on a background — the same app speaking two visual
languages. Each sector is now a console with its own index and frame, its
controls in a tinted bar under the header, its status in the header's
verdict slot, and its content scrolling INSIDE the frame so the page never
scrolls the chrome off the top.

**The chrome does not move.** The header is a fixed 44px and never wraps:
the readouts rebuild every ten seconds, and a value that grew one
character — or the stale-server readout appearing — pushed the bar onto a
second line and moved every sector's whole layout down with it. Values
are set in tabular figures for the same reason, so a changing digit does
not re-measure the readout and nudge its neighbours sideways. And `main`
reserves the scrollbar's gutter always, because a sector that scrolls
being 8px narrower than one that does not made every switch shift
horizontally. Measured across all eight sectors: one header height, one
content top, one content width.

**The rail is icons.** 54px instead of 138: the labels were costing ~84px
of every screen to say what the icon and the sector number already say.
Eight inline SVGs stroked with `currentColor`, so they follow the tube
like everything else — static markup, never built from data, so rule 2 is
untouched. The label still exists and flies out on hover, because a rail
of bare glyphs is a guessing game the first few times.

**The rail carries state.** It runs full height with a footer pinned to
the bottom: halted-or-running, the budget bar, and the read-only mark.
All three exist in the header too, but the rail is what the eye rests on
while reading a sector, and a status you have to look up is a status you
stop checking.

The blinking cursor is now scoped to the live tail alone. Once the notes
moved into headers it was rendering on six of them at once, which reads
as a fault light rather than a heartbeat.

And the content was the real clutter: the activity stream was printing
raw JSON, where braces, quotes and colons are a third of the row and none
of it is information. Events render as fields now, ids truncate to eight
characters (they exist to be matched, not read), and timestamps become
clock times.

**Version skew is the panel's own trap, and it is now loud.** The server
serves its page from disk on every request but imports its Python once,
so editing `queries.py` while the panel runs leaves a NEW page talking to
an OLD API — and a missing field renders as an empty console, which reads
as "all quiet" rather than "I could not tell you". Found live 2026-08-31:
the systems matrix went blank because the running server predated the
`components` field. Two guards, both tested: `/api/status` carries an
`api_version` the page compares against its own `EXPECTED_API` and
reports as a red STALE SERVER readout, and any console with no data says
so explicitly instead of rendering an empty box. **Bump both constants
together whenever a response shape changes.**

Two implementation notes worth keeping:

- **No web framework.** stdlib `http.server` on a `ThreadingHTTPServer`.
  FastAPI + uvicorn would be two dependency trees and an ASGI stack for a
  single-owner localhost tool that reads files. Revisit when Phase C's
  control surfaces outgrow it, not before.
- **The two rules are tested, not just written.** `test_panel_never_writes`
  hits every endpoint and asserts the logs and data stores are
  byte-identical afterwards; `test_the_page_builds_no_html_from_data`
  fails if `innerHTML` (or any sibling sink) ever appears in `app.js`.
  A rule with a test is a rule; a rule in a comment is a hope.

**B — the brain. BUILT (2026-08-31); review ACTIONS still to come.**
Sector 06 draws the whole second brain as one force-directed graph:
what it remembers, who those memories are about, what work is queued,
and what it can do. Four lobes — memory, people, work, skills — with
people anchored at the centre, because memories are ABOUT them and work
BELONGS to them; they are the hub, not a fourth island.

Every edge is evidence, never decoration:

| edge | what it means |
|---|---|
| `synapse` | two facts whose stored embeddings are close (top-3 per fact, floor 0.45) |
| `subject` | this fact is about this person |
| `relation` | a stored triple, head → tail |
| `owns` | this person's scheduled work |
| `managed_by` | the tool family that operates on this kind of task |
| `coactivation` | these two tools fired in the SAME TURN, N times |

`coactivation` is the one worth defending: "these skills work together"
is not guessed from their names, it is the record of them firing in the
same turn, read out of the audit log.

**It is live.** The SSE tail already carries every tool call, so a firing
tool pulses its neuron — the difference between an anatomy diagram and an
EEG. It runs on the same one connection sector 01 uses and costs nothing
extra.

Gestures: drag a neuron (the whole selection moves with it), shift-drag
an empty patch to band-select a group, double-click to select and zoom
into a cluster, wheel to zoom, esc to clear. Selection of one shows the
node with its wiring; selection of many shows a census, because forty
labels is not a reading.

**Cosmos and spiral were built and then removed** (owner's call, same
day). Both were real — PCA-by-meaning and a time spiral — but the brain
subsumes them, and three layouts over the same nodes meant three things
to keep working for one that gets used. The PCA and k-means survive
underneath: they still supply the `group` colouring and the census.

Two implementation notes that cost real debugging:

- **Anchor beats repulsion, or the lobes smear into one cloud.** At
  ANCHOR 0.014 against REPEL 0.00042 the four regions merged — exactly
  the picture a brain view must not produce. 0.055 / 0.00026 separates
  them.
- **Auto-fit must solve the projection, not guess a constant.** The first
  fit used a guessed factor and rendered the whole graph as a dot in an
  empty field. The renderer places a node at `W/2 + (x*size + view.x) *
  scale`; the fit inverts exactly that.

**Sectors are real URLs** (owner's call, 2026-08-31): `/brain`,
`/turns?sort=tokens&turn=<id>`, `/spend?days=31`,
`/brain?colour=group&lobes=memory,person&sel=p:kiaan`. Without them a
reload dropped the reader back on the overview with every filter reset,
and there was no way to hand someone a link to the turn you wanted them
to look at. The server serves the page for any extension-less path inside
the static root — a typo like `/app.cs` stays a hard 404, and a traversal
is refused before the fallback is reached. State that names data (an open
turn, a selection) is applied AFTER that sector's fetch resolves, because
until then the thing it names does not exist. `replaceState` for tweaks
and `pushState` for sector changes, so a filter keystroke does not become
its own back-button step.

**The selection is no longer a dead end.** Pick skills in the brain and
carry them to sector 01 ("watch them firing") or sector 02 ("turns that
used them"); both land as a removable chip and a real URL
(`/turns?tools=email.unread`). This is the honest half of cross-sector
linking — the audit log records which TOOLS a turn called, so that jump
is evidence. There is no fact→turn jump, because nothing in the log
records which facts entered a prompt; inventing one would have been the
easy, wrong move.

**Two findings the graph can make that a list cannot**, both on the
Findings console and both drawn as a dashed hollow ring: an ORPHAN memory
has no synapse above the floor (either genuinely unique or badly
embedded), and a DEAD skill is registered but has never been called —
capability that exists on paper only. At the default floor: 12 orphans,
2 dead (`calendar.update_event`, `email.search`).

**The mesh floor is a control, not a constant.** 0.45 was a number tuned
by eye on 43 facts; it is now a slider that refetches, and it rides in
the URL. It shipped BROKEN in one direction: `_synapses` filtered at the
module default and `brain_graph` filtered again at the parameter, so
raising the floor worked and lowering it could not recover edges the
first filter had already dropped. Two filters for one threshold is one
too many — the floor is now passed down. 0.30 gives 79 synapses and 2
orphans; 0.65 gives 13 and 25. Seeing that range is the point.

**What the findings turned out to be** (chased down 2026-08-31, backup
taken first):

- `kiaan born_on` "contested" was THE PANEL'S OWN FALSE POSITIVE. All
  three tails were the same date in different spellings, and two came
  from SUPERSEDED facts. Fixed twice over: `memory_links` now excludes
  retired facts by default (their relations are history, not wiring),
  and a single fact spelling one answer two ways is reported as a
  VARIANT, not a contradiction. Contested now means what it says —
  different live facts disagreeing.
- `kia_an` vs `kiaan` was real: the extractor slugged the name
  differently on one fact and split the child into two entities. Fixed
  at the source (`kia_an` added as an alias, so future extractions
  resolve through the registry) and the stored row repointed.
- The redundant `12-10-2025` triple was dropped in favour of
  `12_october_2025` — an unambiguous date beats one that reads as
  10 December in another locale.
- Two eval fixtures had leaked into the live memory tree. The active one
  ("Add event 'eval test'...", created by an eval run that morning) was
  retired with `engine.forget`, which keeps it as history. Worth noting
  what it implies: a one-off COMMAND became a long-term "routine" fact,
  which is an extraction-quality question, not just litter.
- Corroborating relations are now ONE edge carrying a source count.
  Three separate facts saying Kiaan is the owner's son is agreement, not
  three relationships.

## Sector 08 — what it actually did (2026-08-31)

The other sectors answer what Kyraan KNOWS, what is SCHEDULED, what it CAN
do and what it COST. `action_log` answers what it DID — to the calendar,
the reminders, the memory — and nothing surfaced it. For an
owner-reviewed system that is the question the whole design exists to
answer, so it was the largest gap in the panel.

Every side-effectful call with its declared inverse, in three states that
must not blur: **undoable** (has an inverse, not yet used), **undone**
(already reversed — no longer undoable, or the panel would invite
reversing it twice), and **irreversible** (no inverse was ever declared).
Filter chips per tool; the inverse is in the tooltip, because "what would
reversing this run" is the thing you want before you decide.

`undoable` is a STATE, not a button. Undoing is a write and goes through
the kernel in Phase C — rule 1.

**Building it found a live data leak.** The table held 2,473 rows and 2,450
of them were from chat ids 90 and 93 — the synthetic chats in
`tests/test_agent_loop.py`. `store/actions.py` had no `MIRROR_ENABLED`
gate, unlike `facts.py` and `promises.py`, so every `pytest` run wrote
real rows into production; the owner had 32 actions against 2,450 fakes.
Fixed at the source (gate added, conftest flips it off suite-wide, the
store's own pg tests opt back in and clean up after themselves), the test
rows purged after a snapshot, and pinned by a test. A full suite run now
leaves the table at 33.

That is the same failure as the eval fixtures in the memory tree, in a
worse place: the undo history is a safety surface, and it has to be the
owner's actions and nobody else's.

## Sector 07 — the machine (2026-08-31)

Two halves of one question, and neither source can answer for the other.

**The OS says what holds MEMORY.** Load per core (the number that travels
between machines — 1.0 is fully committed), memory with macOS's real
"used" (active + wired + compressed; free pages alone are famously
misleading here), disk, battery, and a process table where every row is
tagged with the PART of Kyraan it is: local model, containers, postgres,
redis, the bot, the panel itself. A wall of paths tells you nothing; a
role tells you what to turn off.

**Our audit log says where the TIME goes.** Model calls grouped by model
with wall time as well as tokens. `ps` cannot tell a chosen call from a
degraded fallback, and the log cannot see six resident gigabytes.

The finding on day one, from the two together: **qwen3:8b holds 5.8 GB
resident — 98% of Kyraan's memory footprint — and burns 51% of all model
wall time on 10% of the calls** (16.5s average against nano's 1.75s).
That is the true price of the local fallback, and neither number alone
would have said it.

stdlib and the OS's own tools only. psutil is the obvious dependency and
is deliberately not taken: this reads four numbers and a process table on
one platform, and the panel's argument for existing is that it adds
nothing to the machine it watches. History lives in a bounded deque in
memory, which is why the graph resets on restart — persisting it would
mean the reader starts writing, and rule 1 says it does not.

Two bugs worth keeping the note for:

- `ps -o comm=` gives the EXECUTABLE, so every Python service was
  "Python" and the bot could not be told from the panel. `args=` fixes it,
  and then the parser must be strict — a command line containing newlines
  had a loose split parsing a continuation line as a fake process, which
  picked up whatever role its text happened to match.
- The graph rendered blank because it called `tubeVariants`, which had
  been inlined away during the brain rewrite. A throw inside a
  requestAnimationFrame loop fails silently — no console error the user
  would ever see, just an empty canvas. It is a shared helper again.

**The brain holds all of memory, not the curated half.** It was showing 43
reviewed facts while the store also held 179 episodes, 10 documents (65
chunks) and 7 face templates — all with embeddings, all part of what the
thing remembers. Seven lobes now: facts, recall, documents, faces, people,
work, skills. New edges, each grounded: `spoke` (an episode's
participants), `recalls` (an episode citing the fact it produced — the
strongest link in the store, it says WHY a fact is known), `about` (a
document's subject people), `recognises` (a face template to its person).

Two things fell out of building it. People are now taken from the
REGISTRY rather than from fact subjects, because Kamal and Titu have
enrolled faces and no facts — keyed off subjects alone their faces floated
unlinked. And one face, "Akansha (employee)", links to nobody: enrolled
without a person record, which is a finding rather than a bug in the view.

Cost: the cold graph went to 2.45s, almost all of it an O(n²) set rebuilt
inside the episode loop. 0.55s once that was hoisted, and memoised for
30s — keyed on demo mode, because without that the cache served whichever
graph was built first under the wrong label.

**The brain is three-dimensional** (owner's ask, 2026-08-31), and it
took no library. The simulation gained a z axis — repulsion, springs and
anchors all run in three dimensions, and each lobe's anchor is spread in
depth deliberately, because with every anchor on one plane the orbit only
ever showed a sheet turning edge-on. A camera (yaw, then pitch, then a
perspective divide) projects it; somas draw back-to-front so the front of
the brain occludes the back; near neurons are larger and brighter, far
ones smaller and dimmer. three.js would have been ~600KB of vendored
script — and the CSP forbids a CDN — to do a rotation matrix and a divide.

Gestures moved with the axis: a plain drag on empty space now ORBITS, a
node drag inverts the projection at that node's depth so the neuron stays
under the pointer from any angle, alt-drag pans, shift-drag still bands.
It turns slowly on its own (`spin`, in the URL like everything else) and
pauses whenever the pointer is on a neuron, so a tooltip never drifts out
from under the cursor. The hub on the overview shares the camera — same
graph, same eye. Every existing feature — hover, select, band, search,
signals — survived, because they all read projected screen positions.

**Motion, second pass** (owner: "movement is so fast", "unable to drag to
move top, bottom, left, right", "signals, pulses are not visible"). Three
faults that compounded. The idle spin resumed the instant you let go, so
any angle you set drifted away — it now stays paused for twelve seconds
after the last touch, and turns at ~2°/s rather than ~7.5°/s. Orbit gain
halved. And pan was hidden behind alt-drag, which nobody finds: it is now
right-drag, a `drag: orbit | pan` switch (in the URL), and the arrow keys.
One edge caught by measurement: `lastTouch` initialised to `0`, and
`now − 0 < 12000` held for a page's first twelve seconds, silently
holding the spin off until the page was old enough. It initialises to the
distant past now.

**The thinking is visible.** Only tool calls lit the brain before, as 2px
dots that lived 2.6s — technically drawn, practically invisible. Now each
live event maps to what it literally is: a MODEL CALL is the person's
turn being thought about, so their node fires and the thought runs out
along their memory wiring; EPISODE RAG is the recall lobe being searched,
so that lobe glows (the event carries a count, not which episodes, so the
lobe lights rather than the page inventing neurons); a `memory.*` tool
lights the fact lobe; the reply closes the loop on the person. Pulses are
bigger, glow, and live 4.2s; a fan-out cap samples ~40 of the owner's
~220 edges so a thought reads as a burst, not a wall. A live line under
the controls names the last event and fades over six seconds. Person
nodes carry their chat id server-side for this; `model_call` events carry
no chat, and fall back to the owner, who it is nearly every time. Nothing
fires on a timer — a quiet assistant shows a quiet brain.

Verification note for whoever reads this later: `requestAnimationFrame`
does not fire in a hidden browser pane, so the idle spin cannot be
measured there. It was proven by driving `advanceCamera()` by hand — 100
steps, yaw delta exactly 100 × SPIN_RATE, all guards clear — and, when the
pane was visible, by two frames four seconds apart.

**Found while proving it in a fresh browser:** the `?token=` handshake
only fired on `/`. A deep link such as `/brain?token=…` served the page
(the query token authenticates that one request) and then 401'd its own
`app.css` and `app.js`, which arrive with no token and no cookie — an
unstyled page stuck on "connecting…". It had never shown up because every
earlier check reused a browser that already held the cookie. The
handshake now fires on any page path and bounces to it with only the
token removed; a test pins it.

**Memories are readable, not just plottable.** Owner's question, and a
fair one: memory was the ONE node type the graph never labelled. People
were named, busy skills were named, and the 43 facts — the actual content
of the second brain — were anonymous red dots, legible only by hovering
one you had already found. Two fixes: a Memories console beside the graph
listing every fact as text, newest first, filtered by the search and
clickable to select and fly to its neuron; and on the canvas, a search hit
or a selection now earns its name. Long fact text still stays off by
default, because 43 sentences drawn at once is not a graph.

**Demo data lives in the process, never in a store** (`KYRAAN_PANEL_DEMO=1`,
`src/kyraan/panel/demo.py`). The obvious way to get a bigger brain to
design against is to seed the memory tree, and it is the wrong way: an
ACTIVE fact enters the model's memory block, so a synthetic "favourite
colour is blue" is not decoration — Kyraan would recall it as true and act
on it. Two eval fixtures had already leaked in by exactly that route.

So the generator produces ~240 facts, 8 people, 8 scheduled items and a
deliberate contested pair, entirely in memory, and the payload carries
`demo: true` so the page says DEMO DATA out loud. The vectors are
synthetic but STRUCTURED — one centre per topic plus jitter — because
random noise would have made every layout a blob and exercised none of
the projection, the clustering or the mesh. One seed, so a screenshot
today matches one next week. Run it on a second port beside the real
panel and compare.

Still owed for Phase B: batch approve/reject on the review queue,
conflict resolution, subject assignment, and the per-person visibility
preview. Those are WRITES, so they go through the kernel per rule 1 and
need the Phase C governance round.

**C — control, through the kernel.** Kill switch, tier override,
reminder/task CRUD, and the good one: approving confirm-gated actions
from the panel with the full stashed call rendered as a diff, instead of
a one-word yes in chat.

**D — config and connectors.** `permissions.yaml` editing with the
existing validator run against the draft, a dry-run, and the save landing
as a git commit so every capability change is revertible. The MCP mount
manager lives here — and note it is blocked on the same thing today:
§3d #3 says the client machinery is built but nothing is mounted because
each server needs a governance data-destination row. The panel should
make us *write that row* as part of mounting, not make mounting one
click.

## Rejected

- **A Jarvis chat pane.** Telegram is the channel; §3d #5 wants a
  *second* channel for redundancy (CLI first), not a third face on the
  same one.
- **Rebuilding `scripts/tui.py` in the browser.** The TUI is the
  development view and stays. The panel's Phase A is the *operational*
  view — history and forensics, not a live session monitor.
- **Panel-side writes to Postgres or the file stores.** Rule 1.
- **Public exposure with accounts and roles.** One owner, one token,
  localhost. Multi-user in the panel would need the §4 visibility model
  re-implemented in a second place.

## The trade

plan.md §3d proposes a build order the owner has not committed to. Each
of those items changes what Kyraan can *do*; the panel changes what we
can *see and manage*. Building Phases A+B displaces roughly one of them.

Recommendation: **A + B are worth displacing one item** — days of work,
read-only so they cost no governance round, and they unblock the
200-review gate family stage-2 is waiting on. **C and D queue behind
goal continuity and the first MCP mounts**, because managing capabilities
we have not built yet is premature.
