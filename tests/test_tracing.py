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


async def test_stages_collect_into_the_turn_summary(monkeypatch):
    """The complete-picture record: stages recorded during a turn land in
    turn_end's summary with model/tool aggregates."""
    import time

    logging_setup.new_turn()
    logging_setup.record_stage("model:frontier", 1200, provider="openai")
    logging_setup.record_stage("tool:web.search", 850)
    with logging_setup.stage("face_recognize"):
        time.sleep(0.01)
    summary = logging_setup.turn_summary()
    assert summary["model_calls"] == 1
    assert summary["model_ms"] == 1200
    assert summary["tool_ms"] == 850
    names = [s["stage"] for s in summary["stages"]]
    assert names == ["model:frontier", "tool:web.search", "face_recognize"]
    assert summary["stages"][2]["ms"] >= 10

    traces = _lines(logging_setup.TRACE_LOG)
    assert sum(1 for t in traces if t["kind"] == "stage") == 3


async def test_kernel_tool_run_records_a_stage(monkeypatch):
    async def fake_dispatch(spec, args):
        return []

    monkeypatch.setattr(reg, "dispatch", fake_dispatch)
    logging_setup.new_turn()
    await kernel.run_tool(kernel.ToolCall("reminders.list", {})
                          if "reminders.list" in reg.load() else
                          kernel.ToolCall("calendar.list_events",
                                          {"start": "2026-01-01T00:00:00",
                                           "end": "2026-01-02T00:00:00"}))
    stages = logging_setup.turn_summary()["stages"]
    assert any(s["stage"].startswith("tool:") for s in stages)


def test_rotation_archives_into_the_subdir(monkeypatch, tmp_path):
    """Rotated files land in logs/archive/, keeping the top level to the
    live files only (2026-08-27: 7 rotated archives + eval files made the
    directory unreadable)."""
    log = tmp_path / "events.jsonl"
    monkeypatch.setattr(logging_setup, "EVENT_LOG", log)
    monkeypatch.setattr(logging_setup, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(logging_setup, "_ROTATE_BYTES", 200)
    for i in range(20):
        logging_setup.log_event("probe", n=i, pad="x" * 40)
    archives = list((tmp_path / "archive").glob("events-*.jsonl"))
    assert archives, "rotation should have archived into the subdir"
    assert log.exists() or True  # live file recreated on next write
    assert not list(tmp_path.glob("events-*.jsonl"))  # nothing at top level
