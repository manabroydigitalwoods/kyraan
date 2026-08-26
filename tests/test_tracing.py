"""Turn correlation + flow traces: every event in a turn carries the same
turn_id, tool results carry durations, and traces.jsonl captures the
boundaries and full model I/O."""
import json

from kyraan.control_plane import kernel, logging_setup
from kyraan.tools import registry as reg


def _lines(path):
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


async def test_turn_id_stamps_events_and_tool_durations(monkeypatch):
    async def fake_dispatch(spec, args):
        return {"ok": True}

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    tid = logging_setup.new_turn()
    logging_setup.log_trace("turn_start", chat_id=1, user_text="hi")
    await kernel.run_tool(kernel.ToolCall("reminders.list", {})
                          if "reminders.list" in reg.load() else
                          kernel.ToolCall("calendar.list_events",
                                          {"start": "2026-01-01T00:00:00",
                                           "end": "2026-01-02T00:00:00"}))
    events = _lines(logging_setup.EVENT_LOG)
    tool_events = [e for e in events if e.get("kind") in ("tool_call", "tool_result")]
    assert tool_events and all(e.get("turn_id") == tid for e in tool_events)
    result = next(e for e in tool_events if e["kind"] == "tool_result")
    assert isinstance(result.get("duration_ms"), int)

    traces = _lines(logging_setup.TRACE_LOG)
    assert traces[0]["kind"] == "turn_start" and traces[0]["turn_id"] == tid


async def test_handle_message_writes_turn_boundaries(monkeypatch):
    from kyraan.agents import orchestrator

    async def canned(chat_id, raw_text):
        return "hello!"

    async def no_facts(raw_text, context="", insist=False):
        return []

    monkeypatch.setattr(orchestrator, "_dispatch", canned)
    monkeypatch.setattr(orchestrator.extraction, "propose_from_message", no_facts)
    reply = await orchestrator.handle_message(chat_id=93, raw_text="hi there kyraan")
    assert reply.startswith("hello!")

    traces = _lines(logging_setup.TRACE_LOG)
    start = next(t for t in traces if t["kind"] == "turn_start")
    end = next(t for t in traces if t["kind"] == "turn_end")
    assert start["turn_id"] == end["turn_id"]
    assert start["user_text"] == "hi there kyraan"
    assert end["reply"].startswith("hello!")
    assert isinstance(end["total_ms"], int)


def test_events_without_a_turn_carry_no_turn_id(monkeypatch):
    # a fresh context (no new_turn) must not inherit an id
    import contextvars

    def in_fresh_context():
        logging_setup.log_event("orphan_probe", x=1)

    contextvars.copy_context().run(
        lambda: (logging_setup._turn_id.set(None), in_fresh_context()))
    events = _lines(logging_setup.EVENT_LOG)
    probe = next(e for e in events if e["kind"] == "orphan_probe")
    assert "turn_id" not in probe
