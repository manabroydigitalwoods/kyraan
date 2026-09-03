"""Coding-task model by load, with one escalation (owner 2026-09-03)."""
import asyncio

from kyraan.tools import code_agent as ca


def test_model_follows_task_load_and_explicit_asks():
    assert ca.pick_model("fix the typo in the docstring of scripts/panel.py")[0] == "haiku"
    assert ca.pick_model("add a --json flag to scripts/chat.py output")[0] == "sonnet"
    assert ca.pick_model("refactor the reminder scheduler across modules and fix the DST race")[0] == "opus"
    assert ca.pick_model("touch a.py, b.py and c.py to rename the field")[0] == "opus"      # several files
    assert ca.pick_model("rename the field, use opus")[0] == "opus"                          # explicit wins
    assert ca.pick_model("investigate why the eval is flaky with haiku")[0] == "haiku"
    assert ca.next_rung("haiku") == "sonnet" and ca.next_rung("opus") is None


def test_a_stalled_lower_rung_escalates_once(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "JOBS_PATH", tmp_path / "jobs.json")
    job = {"id": "j2", "chat_id": 1, "task": "t", "branch": "b", "dir": str(tmp_path), "status": "running",
           "model": "haiku", "model_why": "small", "escalated_from": "", "started": "", "finished": "",
           "summary": "", "diffstat": "", "cost_usd": 0.0, "turns": 0, "error": ""}
    ca._save([job])
    calls = []

    def fake_run(j):
        calls.append(j["model"])
        if j["model"] == "haiku":
            return {"rc": 0, "stderr": "", "raw": "",
                    "data": {"is_error": False, "result": "I could not complete this.",
                             "total_cost_usd": 0.05, "num_turns": 3}}
        return {"rc": 0, "stderr": "", "raw": "",
                "data": {"is_error": False, "result": "Done; tests pass.",
                         "total_cost_usd": 0.40, "num_turns": 12}}
    monkeypatch.setattr(ca, "_run_claude", fake_run)
    monkeypatch.setattr(ca, "_git", lambda args, cwd: " x.py | 2 +-" if args[0] == "diff" else "")
    sent = []

    async def send(chat_id, text):
        sent.append(text)
    ca.init(send)
    asyncio.run(ca._run(job))
    done = ca._load()[0]
    assert calls == ["haiku", "sonnet"] and done["status"] == "done"
    assert done["model"] == "sonnet" and done["escalated_from"] == "haiku" and done["cost_usd"] == 0.45
    assert "escalated from haiku" in sent[0]
