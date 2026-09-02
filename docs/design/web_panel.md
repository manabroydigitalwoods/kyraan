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

**Signals ride visible wires, and the log says what was fetched and what
came back** (owner: "signal should pass in the connected string", "what is
Kyraan trying to fetch and what did he get"). The structural wires (spoke,
subject) sit at 10–16% alpha, so a pulse riding one looked like it was
crossing empty space — the string was there, just invisible. A wire now
draws bright for exactly as long as it carries a pulse.

The live feed is the real one: the same SSE tail of `events.jsonl` sector
01 shows. And the events carry the answer to the second question, which
the page was throwing away: `agent_tool_call.consider` is the model's own
stated intent — its WANT / HAVE / NEED line; `tool_call.args` is the
literal query; `tool_result` is ok-or-failed with duration; `episode_rag`
is how many memories came back and how close the best was; `model_call`
is the model and the tokens in. A Live console, first in the side column
because it is the point of watching, keeps the last twelve as a sequence:
TRY, then GOT. What the events do NOT carry is the result body — it only
enters the next prompt, in `traces.jsonl` — so the log reads "ok · 1493ms"
and never pretends to know what came back.

**Signals, third pass** (owner: "very messy", "should pass through the
connector line precisely, like an electric pulse flowing"). Two faults.

The mess was the cascade: forty of the owner's ~220 wires sampled, and
every neuron reached re-firing its neighbours two hops deep — one thought
was a firework, and a firework tells you nothing. Each event now sends a
FEW pulses along the wires it literally means: a model call reads the
person's own subject wires into the fact lobe (six); recall reaches along
the spoke wires, as many as episodes came back (two even when none did —
it looked); a tool lights the two or three it habitually fires with. No
cascade unless asked for, and then one hop along the mesh. Nine pulses in
flight for a whole turn, down from eighty-one.

The call itself is a wire the store does not have — nothing links a
person to a skill — yet "this person's turn called this tool" is exactly
what the event says. So a call is drawn as a transient wire: out to the
skill when tried, back to the person when the result comes, one pulse
riding each way. That is the story the firework hid: asked, reached,
answered.

The imprecision was a real bug. A curved wire's bow flips side depending
on which endpoint is "a", and a pulse travelling backwards along a wire
built its curve from its own travel direction — so every reverse pulse
rode a curve mirrored to the wrong side of its wire. Measured on a 332px
reverse wire: the old head sat 39.9px off the drawn curve at mid-wire
(exactly 2 × 0.12 × L/2), the new one 0.00px at every sample. Pulses now
carry the wire's own orientation and walk it either way. They render as
current — a charged stretch of wire brightening and thickening toward a
white-hot head — because a dot with a short tail read as a dot.

**Signals come back.** A recall is a round trip: the thought reaches
into memory and what it finds returns. A pulse sent with `bounce` walks
its wire out, and on arrival one return pulse walks the same wire — same
orientation — back; the return never bounces again, so a thought is out
and back, never a ping-pong. Model calls read the person's fact wires the
same way. Proven with the clock driven by hand (the harness pane is
hidden, so the frame loop is stalled — `t` sat unchanged for 600ms):
three out, three back, all flagged not-to-bounce, zero in flight after.

**Space** (owner: "we need more space, manage the spaces"). The right
column was six frames, most empty most of the time, and a legend box sat
on the canvas. Now: each side console folds on its header and the fold is
remembered; an empty console shrinks to one quiet line; the whole column
hides behind `panels ▸` (in the URL as `side=0`) and the canvas takes the
width — measured 1491px → 1803px; the lobes checklist carries each lobe's
swatch and count in lobe colour, so the legend box only appears for the
colourings the picker cannot show; the fit margin came down from 56px to
30px. The eleven inline lobe and wiring checkboxes became two dropdown
checklists showing counts (`lobes 6/7` is the only hint something is
hidden once the boxes are out of sight), which put the camera controls
back on the first row.

The controls row hides too (`controls ▴`, in the URL as `top=0`), and both
toggles live in the sector header rather than the row, so hiding the row
cannot take the way back with it. Neither toggle re-frames: the view is
centre-relative, so the graph keeps its exact place, size and simulation
state and the freed space opens around it — the refit that used to run on
toggle read as "the simulation reset" (measured: node position, scale and
alpha byte-identical through both toggles). One thing the measurement
caught: the canvas height was a fixed `calc(100vh − 190px)`, so hiding
the row freed 43px above the canvas that the canvas never took; the
layout is flex-filled now, top to bottom.

Then the corners (owner: "adjust to the monitor screen, minus the top-left
and bottom-right spaces"). Measured at 1600×900: 12px above the console,
14px left of it, 25px to the right (padding plus the reserved scrollbar
gutter), 12px below, and a 30px page footer whose only content —
"read-only" — the rail already shows as RO. `main` padding is 6/8 now,
the footer is gone and its sentence is the RO mark's tooltip, and the
layout gap is 8px. The gutter stays: it is what keeps sectors from
shifting sideways when one scrolls and another does not, and 11px is a
fair price for that. Canvas at 1600×900: 727px, +43px with the controls
hidden, +312px wide with the panels hidden.

Two URL bugs the picker flushed out, both pre-existing. `lobes` was
written only when fewer than four were visible — the count from when
there were four lobe types; with seven, hiding one left six and nothing
was ever recorded. And the URL writer was registered before each
control's state listener, so it read the previous state: hiding wrote
nothing, restoring wrote "hidden" — one step late, for every control in
that list. It defers a tick now.

**Obsidian notes are in the brain, with a way back to the vault** (owner:
"did we add Obsidian link in brain?"). The owner had wired the vault into
memory in three commits — notes join the document index as `kind='note'`
with the people they are about, their `#tags` and `relation:` lines, an
event date and a vault-relative path; a person-note registers its
person — and the brain showed none of it. Notes get their own lobe: a
note is something the OWNER wrote, a different kind of memory from a
photo Kyraan was sent. Edges are what the index stores: note → person
(`about`), and note → tag (`tagged`), where a tag becomes a hub only when
it joins two or more notes — one note's private tag is a Selection detail,
not a neuron. Note-to-note wikilinks are not stored by the index, so none
are drawn. A superseded or deleted note is kept, dimmed, like a
superseded fact.

Every note carries `obsidian://open?vault=<folder>&file=<path>` built
server-side and tested, and the Selection panel offers it as "in
Obsidian ↗" — the only place it appears. Facts get no such link: the
memory tree is not inside the vault (`memory tree inside vault: False`
when measured), and a link that opens nothing is worse than a path.
`note_indexed`, `vault_synced` and `person_registered_from_note`
merge-refresh the graph like any other store change. Today: two indexed
notes, both superseded, one with entities — the vault sync has run once.

**From a phone on the same network** (owner's question). Two answers,
and they are not equal. The intended path is Tailscale: encrypted,
device-bound, and the panel stays on loopback. The same-network path is
`scripts/panel.py --lan` (`--host 0.0.0.0`), which prints the phone-facing
URL — but over plain Wi-Fi the token travels in clear, on a network that
also carries the house's IoT and any guest, and the panel reads memory
facts, contact numbers and mail subjects; the startup banner says so.
Three things had to change for the LAN path to work at all: the Host
allowlist added only the bind address, so a phone's `Host:
192.168.0.166` was a 421 that looked like DNS rebinding — a network bind
now allows the machine's own addresses (a loopback bind still adds
nothing); the brain had no touch handlers, so it rendered on a phone and
could not be turned — one finger orbits (or pans in pan mode), two
fingers pan and pinch-zoom about their midpoint, a tap selects, and the
canvas owns its touches so the page never scrolls; and the header could
not hold four readouts on a phone's width — two lines, still a fixed
height.

**Hover focus, the way Obsidian's graph does it** (owner: "when I hover a
point, highlight only the connected points and links and dim the
others"). The hovered neuron, its neighbours over visible wires, and the
wires between them stay lit; everything else falls back to 10%, wires
to 6%. The focus set is built once per hovered node, not per frame, and
the dimming is multiplied through the same colour path search-dimming
uses, so halos, somas and rings all fall back together. Neighbours name
themselves while focused — that is half the point of the gesture. Eased
in and out (0.28 of the remaining distance per frame) so the graph does
not snap. Measured on `kiaan`: 15 in the set, neighbours 1.0, outsiders
0.10, touching wires 1.0, strangers 0.06, and everything back to 1.0 with
the focus cleared on release.

**One note, one neuron** (owner: "are we duplicating Obsidian notes
whenever we index?"). Four "Rakesh Chakraborty" squares looked like it.
The rows said otherwise: four versions with four distinct hashes — the
note was edited four times between syncs — and `index_file` returns
`unchanged` when the live row's hash matches, so the indexer keeps one
row per EDIT and supersedes the old one. History, by design. The
duplication was the brain's, twice: it drew every version as a neuron,
and it read "superseded" as `IS NOT NULL` when the index marks a live
row with an EMPTY array, so even the current version was dimmed as dead.
Now: one neuron per vault path — the live version, or the newest dimmed
as "gone from the vault" if every version is superseded — with the
version count in Selection.

**Contacts, wired by evidence** (owner: "we have contacts, can we connect
them with the brain?"). 395 entries in the book — more than the brain
held in total — and measured first: 7 resolve to a registry person, none
match an enrolled face, none name a relation entity. And one of the seven
is false: "Suman Sutradhar" resolved to `suman_ghosh` on first name
alone. So a contact becomes a neuron only where it provably names a
registry person: an exact full-name match is a solid `is` wire; a match
on one alias token ("Habu New" → kamal via the alias "habu") is a dashed
`maybe` wire, listed in Findings as a candidate to confirm, never
asserted. The other 388 stay out of the graph — dropping them in would
have drowned it — and answer the search box instead, marked "outside",
from a Contact book console that reads the store's own `find`.

Phones and emails appear in the Selection panel and nowhere else. The
store's rule is that they leave only through a direct reply and never
toward a model; the canvas is the closest thing this panel has to a
prompt, so they never become a label or a tooltip on it. A
`contacts_synced` event merge-refreshes the graph like any other store
change. The lobe caption reads "CONTACTS 7 of 395", which is the finding:
the book is almost entirely outside the brain.

**The turn card** (owner: "when live, show a popup: what is Kyraan trying
to do, what is he finding, what he got, how much effort, the steps he
followed"). Every stream event is stamped with a `turn_id`, so a whole
turn assembles itself live into one card on the canvas, top-right, out
of the graph's way. What was ASKED comes from the trace — the events do
not carry the user's words, so the card makes one `/api/turn` fetch when
a turn begins, and one more when it ends for the REPLY. Between them,
each step as it happens: THINK (model, tier, tokens in/out, latency), GOT
(recall → n episodes, best match), TRY (the tool, with the model's own
WANT/HAVE/NEED line, then the literal args), GOT (ok · ms, or failed with
the error), FIX (any rail correction — contract, deflection, tier
fallback, loop guard), REPLY (steps, tier). Then the effort: model calls,
tools, tokens, cost, model time, wall time, corrections — sums of that
turn's own events, nothing estimated.

It lingers twelve seconds after the reply, stays while the pointer is on
it, pins, closes, opens the turn in forensics from its id, and follows
the reader between the brain and the overview hub. A new `turn_id`
starts a fresh card. Built node by node — the no-HTML-from-data rule
holds for the card as for everything else, and the card is made entirely
of event text.

Proven on a real turn id with a synthetic eight-event sequence stamped
onto it: asked and replied text from the real trace, phases in order,
effort cells summed, pin/close/follow/reset each measured.

**New memories appear without a reload** (owner: "do I need to reload the
page to see new memories?"). The answer was yes: the graph was fetched
once per page load and the server memoised it for 30s, so a fact promoted
or an episode ingested after the page opened did not exist in the brain
until a refresh. Now a store-changing event on the stream — the same
stream that lights the neurons: `memory_promoted_via_chat`,
`memory_forgotten`, `episodes_ingested`, `document_ingested`,
`face_enrolled`, `person_enrolled` and their kin — schedules one refetch
2.5s later (the write lands, a burst coalesces) with `fresh=1`, which
bypasses the memo. The result is MERGED, not reloaded: every neuron the
reader can already see keeps its exact position, new ones are seeded at
their lobe's edge and lit as they arrive, gone ones leave the selection.
A reload would have re-seeded the whole layout — the "reset" just asked
to be rid of.

**Search by key** (owner: "use cmd+space to search"): Cmd+Space, Ctrl+Space
or `/` focus the neuron search and reveal the controls row if it is
hidden. Bound honestly: on a Mac the OS owns Cmd+Space (Spotlight) and the
page usually never receives it, which is why the other two exist. Esc in
the box clears it and hands focus back to the canvas, so the next Space
is a pan and not a character.

**Keys** (owner's ask): plain drag orbits; Space+drag pans (the hand);
Cmd/Ctrl+drag zooms about where you pressed (up = in, recomputed from the
start so it never drifts); Shift+drag selects; wheel zooms at the cursor;
`+`/`−` zoom about the centre; `0` fits; arrows pan; Esc clears. Held
modifiers take precedence over grabbing a neuron, so you can pan across a
dense lobe without picking one up, and they clear on blur — a key held
when the window lost focus would otherwise stick forever. The map lives
in the drag control's tooltip, since the hint line was removed by request.

Zoom is about the cursor now. The renderer places a node at
`W/2 + (c·size + view.x)·scale`, so holding a screen offset K fixed across
a scale change means shifting the view by `K·(1/s₁ − 1/s₀)`. Measured: a
node 47px off-centre drifted 0.4px through a 0.30 → 0.49 zoom.

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

Proof on a 375×812 phone viewport (hidden pane, synthetic TouchEvents, hand-driven because the pane stalls RAF): header wraps to 86px, no horizontal scroll, canvas `touch-action: none`, canvas 485px tall; one-finger drag orbits (yaw +0.264, pitch +0.132), two-finger pinch scales ×2.25 about the midpoint, a tap on a person node selects it; no console errors. LAN bind verified at the wire: phone `Host` → 200, mDNS name → 200, foreign host → 421, and after restarting on loopback the LAN address refuses the connection.

## Phone-friendly views

The desktop model is "the shell never scrolls, the consoles do". On a 375px screen that crushed the overview's five consoles to 2px each (bodies 0px) because nothing fits in one screen, the rail's ten children needed 394px, the header burned 86px on four readouts, every list clipped its right-hand columns (the `.kind` column alone reserved 178px of a 359px row), spend's rows were 140px tall because the model list wrapped, and the brain's controls took 400px above a 485px canvas.

One `@media (max-width: 720px)` section now holds the phone model: header is a 2×2 readout grid at a fixed 60px (the jump rule still applies); the rail is nine equal slots (375px, no scroll); overview and host scroll as a page with natural-height consoles (`flex: 0 0 auto`, host column `align-items: stretch` — `start` sized console 09 160px narrower than 08); the list sectors keep scrolling inside their frame. Columns a phone drops: turns' ms and tools (in the detail, one tap away), the stream's turn-id chip, spend's cached and models. The brain starts with controls and side consoles folded (app.js, `PHONE.matches`; the header toggles bring them back, and on a phone the URL names the OPEN state — `top=1`/`side=1` — because folded is the default there).

Measured at 375×812: header 60px, rail 43px and 375px wide, overview console bodies 190/141/277/162/200/150px, hub 471px with a one-line caption, turns/stream/schedule/actions/host rows 0 overflowing, spend rows 30px with 5 columns, brain canvas 650px (was 485), graph centre within 2px of canvas centre after fit, host consoles all 363px.

Orbit direction, set by hand: `YAW_GAIN` is negative — a sideways drag turns the graph (drag right, near face goes right); `PITCH_GAIN` stays positive — a vertical drag tilts the camera (drag down, look down onto the top). Both signs cover mouse and touch. Proof: a right-and-down touch drag gives yaw −0.132, pitch +0.066.

## The core, and documents on the mesh

The server already put Kyraan at the centre (`k:kyraan`, type `core`, wired `acts` to every skill, `fires` to scheduled work, `received` from every file and note, `talks` with the owner) — the client dropped it because `core` was not a lobe it knew. Now: a lobe pinned at the origin (the simulation zeroes it every step; people moved to (0, 0.32, 0.12) so they orbit it), always shown, the tube's own colour in every colour mode, a white-hot soma with two rings and its name under it, radius 12, and a Selection card that counts its wiring. Live signals route through it: model call = owner → core, tool ask = core → skill, tool result = skill → core, reply = core → owner, a reminder = core → that task. Scheduled runs finally have a source: their wires used to need a person. `API_VERSION` 12 so a stale page cannot silently hide the core again. Proof: core at world (0,0,0), screen offset from canvas centre (0,0); a synthetic turn produced the four wires `p:owner → k:kyraan → s:home.get_state → k:kyraan → p:owner`.

Documents (owner: "some documents are not linked yet, they might have connections"): 5 of 21 had nothing but their wire to the core, 9 more had one edge. Every chunk carries the same 384-d embedding a fact does, and the mesh ignored them. A document's vector is the mean of its chunks' unit vectors; documents and notes are meshed with the facts, the episodes and each other under the same top-3 / floor rule, keeping only edges that touch a document (the other meshes exist already). Live: 0 documents left isolated; 79 document synapses (11 to facts, 12 to episodes, 56 document–document); manab.pdf 0 → 3, suman.pdf 0 → 5, each 31 Aug moment 0 → 3–4. Only "Online Payment" (one about-edge) and "Ruma pain gel" (two) stay thin: nothing in the store is near them, which is itself the reading.

Selection focus (owner, 2026-09-03, on the phone: "selection links should highlight and others should dim"): the focus followed hover only, and a phone has no hover. It now follows the hovered neuron, else the selection (any size: the union of neighbourhoods; wires touching any selected head stay lit). On the desktop that means a click keeps its neighbourhood lit after the mouse leaves; Esc clears. Proof at 375×812 with a synthetic tap on the owner: focusMix 1, selected and neighbour alpha 1, stranger 0.10, touching wire 1, other wire 0.06; clearing the selection returns mix to 0.

## The core beats; two fingers roll

Heartbeat (owner, 2026-09-03: "put some effect on the Kyraan point, it will look like it's alive, a different colour, heartbeat type"): the core has its own colour, `--core` per tube (ice `#8fefff` on amber, violet on green, rose on blue) — not a lobe colour, because it is not a lobe. `coreBeat()` is lub-dub-rest at 1150ms; 650ms and a wider, heavier ring while the core has fired in the last four seconds, so the beat quickens when it is working — the one thing the beat says that the wires do not. The soma swells 22% with each beat, a ring leaves it once per cycle. Reduced-motion: no beat. Proof: palette core = hsl(189, 100%, 78%) from `--core`; quick=false at rest, true after a fire; amplitude 0 → 0.32 across a cycle.

Two-finger roll (owner: "two finger rotation will be convenient"): `brainCam.roll`, a turn about the viewing axis applied in the camera plane before the pan offset (a pan still follows the finger whichever way the brain is rolled; `screenDeltaToWorld` undoes the roll first so a dragged neuron follows too). The twist is the change in the two fingers' angle, wrapped at ±π, recorded from touchstart so the first move counts; the first cut lost it (NaN roll on the first move — froze the camera). Reset zeroes it. Proof: a 60° twist → roll 1.047, a person node turned 1.047 about the core, its distance to the core unchanged, the core did not move, reset → 0.

Turn card on a phone (owner: "we can compact this popup for mobile"): 10px type, tighter rows, the six effort figures on one line, capped at 38% of the canvas and scrolling inside. A full five-step turn with details measures 205px on a 665px canvas (31%); the two-item card in the owner's screenshot took more than that before.

Viewport and the tucked status bar (owner: "fix the viewport for mobile view; top panel on mobile should be auto hidden"): `height: 100dvh` with a `100vh` fallback (on iOS Safari 100vh includes the strip under the toolbar, so the bottom of every sector sat behind it), `viewport-fit=cover` and a safe-area bottom pad. On a phone the header shows for five seconds on load or on a tap of the 14px handle above the rail, then folds to 0px and the brain takes the 60px; the readouts keep updating underneath. Proof: body 812 = innerHeight, handle 14px, not tucked at load, tucked after idle, shown again on tap.

Three phone fixes (owner, 2026-09-03): the overview's parked schedule console drew as an empty frame because `.console { display: flex }` outranked the browser's `[hidden]` rule — one `[hidden] { display: none !important }`; every notched box (console, hub, picker menu, turn card) now paints a 1px diagonal just inside its clip-path cut, from one shared `--notch-edge` gradient sized to each notch (11/14/9/11px), where before the border simply stopped either side of a dark triangle; and phone type comes down from 13px to 11.5px with the chrome's absolute sizes following.

## Brain view, next round (owner: "make more improvements")

Four, each measured at 375×812: (1) node labels no longer overprint — a label that would land on one already drawn this frame is skipped unless asked for (selected, hovered, searched); the skill lobe went from a dozen names on top of each other to 8 drawn, 13 skipped. (2) A synapse is as bright as it is strong: `edgeStrength` maps cosine from the floor (0.45 → 0.45 alpha) to 0.95+ (1.0), width follows; structural wires stay flat. (3) When the side consoles are folded (a phone), a tapped neuron gets a two-line callout on the canvas — name, then "type · N wires · double-tap zooms in" — so a tap tells you what you tapped without opening the panels. (4) Double-tap: on a neuron, zoom to what it is wired to (its focus set; owner → scale 0.75 → 0.87); on empty space, fit the whole brain (→ 0.70). Desktop double-click keeps zooming to the colour group. Also: a focused neighbourhood is named only when it has ≤ 24 members or the view is zoomed past 1.7× — the owner's 251 neighbours as text was a wall.
