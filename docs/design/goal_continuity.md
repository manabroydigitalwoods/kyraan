# Goal Continuity — design record (decided 2026-08-31)

The gap (§3d #1): Kyraan can run a moment ("8 PM: check the calendar")
but not a pursuit ("plan Kiaan's birthday"). Anything spanning days
lives in the owner's head and every conversation restarts from zero.

## Owner decisions (2026-08-31, in-chat)

- **Autonomy:** research + propose. Work cycles are read-only; they may
  SUGGEST confirm-gated actions in their report, never take them.
- **Proactivity:** ping only on real progress, plus a one-line status in
  the owner's morning brief while goals are active. Never a nag.
- **Caps:** 3 active goals per person; cycles capped at 1/day (hard max
  2) — at read-only step caps that bounds spend well under the agreed
  ~$0.50/day/goal.
- **Who:** owner AND enrolled adults (the owner chose beyond the
  owner-only recommendation). Consequences accepted into v1:
  - goals are strictly chat-scoped, like reminders and documents — the
    owner does NOT see an enrolled person's goals;
  - a person's work cycle runs AS THAT PERSON: viewer context is set to
    their person+stage for the whole run, so tool reach and §4 fact
    visibility are theirs, never the owner's (the contextvar default is
    owner — a goal cycle must never inherit it);
  - goal tools join the `full` stage toolset (frozen-surface test
    updated in the same commit, per the two-place rule);
  - progress pings deliver through the enrolled-chat path with the
    standard delivery truth.

## Shape

A Goal: id, chat_id, person, stage, title, why, steps
[{text, done, note}], journal [{ts, text}], status
(active|paused|done), cadence_hours (default 24), next_cycle_iso,
cycles_today. File store `data/goals.json` (rides the nightly tar; PG
mirror joins when goals prove themselves — same note as event_rules).

Two ways forward:
- **Conversationally** — goals.update / goals.set_status during any
  turn; checking a step or adding a note is internal state, auto
  permission. Creation is confirm-gated (a standing autonomous
  behavior deserves a yes).
- **Work cycles** — on cadence, a read-only agent run gets the goal +
  journal and researches the open steps. Result handling: a report of
  NOTHING_NEW is dropped silently (edge doctrine); real findings append
  to the journal and ping the person. Delivery truth: the journal keeps
  the finding even if the ping fails; the ping retries next cycle via
  the undelivered marker.

Tools (5, kept few to hold the menu small): goals.create (confirm),
goals.list, goals.show, goals.update (step done / add step / note),
goals.set_status (paused|active|done — the undo inverses live here).

## Rejected

- Autonomous writes from cycles (pre-approved action lists) — needs its
  own governance round; "research + propose" was chosen explicitly.
- A separate planner/decomposer stage — the loop IS the planner
  (phase3_architecture §1); the model maintains steps through the same
  tools the owner uses.
- Owner visibility into others' goals — contradicts the §4 privacy
  posture that reminders and documents already follow.
