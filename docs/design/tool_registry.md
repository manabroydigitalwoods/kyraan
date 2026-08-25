# Tool Registry — Phase 2 Design

Status: **draft for review** — no code yet, per the plan's own sequencing
("build the tool registry … before adding individual tools", plan.md §5
Phase 2). Everything here is open to veto before implementation starts.

## What this is

The registry is the single place every tool Kyraan can call is declared:
what it does, what it takes, what it's allowed to do, and what happens
when it fails. Skills and agents reference tools from here — no tool
exists outside it, exactly like the kernel already refuses to run an
unregistered skill.

## Decisions (and why)

### 1. Declared in `config/permissions.yaml`, under a new `tools:` section

One file remains the complete audit surface for *everything that has a
permission level* — skills and tools side by side. Splitting into a
separate `tools.yaml` is a mechanical change we can make when the section
outgrows the file; starting split would cost a second loader and a second
place to check now, for no present benefit.

### 2. One entry per narrow tool

```yaml
tools:
  calendar.list_events:
    description: List events between two times on the owner's calendar.
    server: calendar          # which adapter/MCP server provides it
    permission: auto          # auto | confirm | disabled
    side_effects: read        # read | write | notify
    params:
      start: {type: datetime, required: true}
      end:   {type: datetime, required: true}
    returns: list of {title, start, end, location?}
    failure:
      retries: 2              # transient errors only (same classification as the router)
      timeout_s: 10
      on_failure: surface     # surface | fallback:<tool.name> | silent

  calendar.create_event:
    description: Create one event on the owner's calendar.
    server: calendar
    permission: confirm       # write ⇒ confirm, enforced at load (see rule below)
    side_effects: write
    params:
      title: {type: string, required: true}
      start: {type: datetime, required: true}
      end:   {type: datetime, required: true}
    returns: created event id
    failure: {retries: 0, timeout_s: 15, on_failure: surface}
```

Narrow, single-purpose tools (`calendar.list_events`, not `calendar.do`) —
easier for a model to call correctly, and permissionable precisely.

### 3. Hard rule, validated at config load: `side_effects: write|notify ⇒ permission: confirm`

A misconfigured write tool with `auto` permission must be impossible, not
merely discouraged. `registry.load()` fails fast with a clear error if any
entry violates this. Relaxing a specific tool to `auto` later is a Phase 4
decision (per-skill guardrail maturity), made by loosening the validator
for an explicit allowlist — never by default.

`disabled` exists so a tool can be parked without deleting its entry
(and its audit history).

### 4. Execution goes through the kernel, like everything else

New `kernel.run_tool(call, executor)` mirroring `run_skill`:

1. Kill switch check (blocks, logs).
2. Permission check — `confirm` without approval raises the existing
   `ConfirmationRequired`; the orchestrator's already-built-and-tested
   confirm flow ("reply yes/no") handles the user side unchanged.
3. Param validation against the entry's `params` schema — a malformed
   model-generated call is rejected *before* it reaches the adapter.
4. `tool_call` / `tool_result` events to the audit log.
5. Timeout + retry per the entry's `failure` policy; on exhaustion, apply
   `on_failure`: **surface** (tell the user honestly — the default),
   **fallback** (invoke a named alternative tool once), or **silent**
   (log only — legal solely for `notify`-class tools).

### 5. Transport-pluggable adapters, MCP-shaped

Each `server:` name maps to an adapter implementing one interface:

```python
class ToolAdapter(Protocol):
    async def call(self, tool_name: str, args: dict) -> object: ...
```

Declared alongside the tools:

```yaml
tool_servers:
  calendar:
    transport: builtin        # builtin | mcp-stdio
    module: kyraan.tools.calendar_caldav
  # later, e.g.:
  # home_assistant:
  #   transport: mcp-stdio
  #   command: ["uvx", "home-assistant-mcp"]
```

MCP is the standard for anything external (per plan §3b), but the first
calendar adapter can be in-process (`builtin`) behind the same interface —
swapping it for a real MCP server later changes config, not callers.

### 6. Loop engineering, minimal Phase 2 slice

Full agentic tool-choice comes later; Phase 2 skills use
**plan-then-execute** with hard rails (plan §3b):

- `max_steps: 5` per skill invocation, absolute.
- Loop detection: the same `(tool, args)` pair repeating breaks the loop
  and asks the user instead of spinning.
- Human checkpoint **mid-loop**: a `confirm` tool inside a chain pauses at
  that step (the existing confirm flow), not after the chain finishes.

### 7. Testing contract

- Registry: load-validation tests (write-tool-with-auto fails, unknown
  server fails, malformed params schema fails).
- Kernel: `run_tool` gating tests mirroring the existing `run_skill` set.
- Each adapter: testable in isolation against a fake transport; a
  `FakeAdapter` ships in the test helpers so tool chains are testable
  without any real calendar/network.

## First tool: calendar — one open input needed

Recommendation: a **CalDAV** adapter as the built-in first server. One
protocol covers iCloud, Google, and Fastmail calendars (all speak CalDAV
with an app password / app-specific credentials), fits the self-hosted
principle (no vendor SDK, no OAuth dance for v1), and the `python-caldav`
library is mature.

**Needed from Manab before implementation:** which calendar the family
actually lives on — iCloud, Google, or other. That decides the setup
instructions and which quirks to test against; the adapter code is the
same either way.

Rollout honors the soak gate: `calendar.list_events` (read) can go live
early; `calendar.create_event` (write, confirm-gated) waits until Phase 1
has its weeks of quiet running and the token hygiene is done.

## Explicitly out of scope for this design

- Free-form agentic tool selection by the model (needs the eval harness
  first — Phase 4).
- Home Assistant / Woodsportal tools (next after calendar, same registry).
- Relaxing any write tool to `auto` (Phase 4 guardrail maturity).
