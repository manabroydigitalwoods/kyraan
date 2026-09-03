"""Coding tasks delegated to Claude Code (owner 2026-09-03, option 2)."""
import asyncio
import json

import pytest

from kyraan.tools import code_agent as ca


def test_prompt_and_env_keep_the_agent_contained():
    p = ca.prompt_for("add a --json flag to scripts/chat.py", "kyraan-agent/x")
    assert "DO NOT commit or push" in p and "never read .env" in p and "kyraan-agent/x" in p
    env = ca._clean_env()
    assert "HASS_TOKEN" not in env and "OPENAI_API_KEY" not in env and "HOME" in env
    assert all(t.startswith(("Read", "Edit", "Write", "Grep", "Glob", "Bash(git ", "Bash(.venv/bin/python")) for t in ca.ALLOWED_TOOLS)
    assert ca._slug("Add a --json flag to scripts/chat.py!") == "add-a-json-flag-to"


def test_run_records_summary_diffstat_and_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "JOBS_PATH", tmp_path / "jobs.json")
    job = {"id": "j1", "chat_id": 1, "task": "t", "branch": "kyraan-agent/j1", "dir": str(tmp_path),
           "status": "running", "started": "", "finished": "", "summary": "", "diffstat": "",
           "cost_usd": 0.0, "turns": 0, "error": ""}
    ca._save([job])
    monkeypatch.setattr(ca, "_run_claude", lambda j: {"rc": 0, "stderr": "", "raw": "",
                        "data": {"is_error": False, "result": "Added the flag; 3 tests pass.", "total_cost_usd": 0.42, "num_turns": 7}})
    monkeypatch.setattr(ca, "_git", lambda args, cwd: " scripts/chat.py | 12 ++++--\n 1 file changed" if args[0] == "diff" else "")
    sent = []
    async def send(chat_id, text): sent.append((chat_id, text))
    ca.init(send)
    asyncio.run(ca._run(job))
    done = ca._load()[0]
    assert done["status"] == "done" and done["cost_usd"] == 0.42 and "chat.py" in done["diffstat"]
    assert sent and sent[0][0] == 1 and "Coding task done" in sent[0][1] and "code diff" in sent[0][1]
    # a failure keeps the branch and says why
    monkeypatch.setattr(ca, "_run_claude", lambda j: {"rc": 1, "stderr": "boom", "raw": "", "data": {}})
    ca._save([dict(job, status="running")])
    asyncio.run(ca._run(job))
    assert ca._load()[0]["status"] == "failed" and "boom" in ca._load()[0]["error"]


def test_one_job_at_a_time_and_short_tasks_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "JOBS_PATH", tmp_path / "jobs.json")
    monkeypatch.setattr(ca, "available", lambda: True)
    ca._save([{"id": "j", "status": "running", "task": "x", "branch": "b", "dir": "d", "chat_id": 1}])
    with pytest.raises(ca.ToolError, match="already running"):
        ca.start(1, "add a json flag to the chat script")
    with pytest.raises(ca.ToolError, match="sentence"):
        ca.start(1, "fix")


def test_code_rail_asks_before_starting(monkeypatch):
    from kyraan.agents import orchestrator
    from kyraan.control_plane import kernel
    monkeypatch.setattr(kernel, "viewer_person", lambda: "owner")
    asked = []
    async def fake_gated(chat_id, call, handler, describe="", **k):
        asked.append((call.skill_name, call.args, describe)); return "ASK"
    monkeypatch.setattr(orchestrator, "_gated", fake_gated)
    out = asyncio.run(orchestrator.handle_message(1, "code: add a --json flag to scripts/chat.py"))
    assert out == "ASK" and asked[0][0] == "code.task" and "Claude Code" in asked[0][2]
    assert "--json flag" in asked[0][1]["task"]
