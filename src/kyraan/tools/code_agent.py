"""Coding tasks delegated to Claude Code (owner 2026-09-03: "does Kyraan
have coding skills like Cursor/Claude/Codex? — go with option 2 for the
kyraan2.0 repo").

Kyraan is the messenger, not the coder. A task runs in Claude Code
(`claude -p`, headless) inside its OWN git worktree of the repo, on a
fresh branch, with a fixed tool allowlist: read, search, edit, write,
`git diff/status/log`, and pytest. Nothing else — no network, no other
shell, no push, no commit. The worktree has no .env (secrets never
reach the agent) and a symlinked .venv so tests run. The job runs in
the background; when it ends the owner gets a message with the
agent's summary, the diff stat and the branch. The owner reviews the
branch (`code diff`) and merges or discards it — main is never touched
by the agent.

Governance: owner-only, confirm-gated (a write), one job at a time,
capped turns and wall clock, spend reported per job.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from kyraan.control_plane.filelock import atomic_write_text, locked
from kyraan.control_plane.logging_setup import log_event
from kyraan.tools.registry import ToolError

REPO = Path(os.environ.get("KYRAAN_CODE_REPO", "/Users/manabroy/workspace/kyraan2.0")).resolve()
WORKTREES = REPO.parent / f"{REPO.name}-agent"
JOBS_PATH = REPO / "data" / "code_jobs.json"
MAX_TURNS = 40
TIMEOUT_S = 25 * 60
ALLOWED_TOOLS = ["Read", "Edit", "Write", "Grep", "Glob",
                 "Bash(git diff*)", "Bash(git status*)", "Bash(git log*)",
                 "Bash(.venv/bin/python -m pytest*)", "Bash(.venv/bin/python -c*)"]
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "SHELL", "TMPDIR")

_send_fn = None
_running: dict = {}   # job_id -> asyncio.Task (process memory)

# Model by task load (owner 2026-09-03: "coding task model switch is very
# important as per task load and tough task"). Three rungs; a task is
# scored by what it says, an explicit "(use opus)" wins, and a failure
# on a lower rung is retried ONCE on the next rung up in the same
# worktree, so the stronger model continues the weaker one's work.
LADDER = ["haiku", "sonnet", "opus"]
_HARD = re.compile(
    r"\b(?:refactor|redesign|rearchitect|architecture|migrat\w*|across|multiple|modules?|"
    r"concurren\w*|race|deadlock|security|performance|optimi[sz]e|investigate|debug|"
    r"root\s+cause|why\s+does|flaky|intermittent|end[- ]to[- ]end|integration|"
    r"database|migration|schema|protocol|async|threading|every|all\s+(?:the\s+)?(?:tools|tests|files))\b",
    re.IGNORECASE)
_EASY = re.compile(
    r"\b(?:typo|docstring|comment|rename|spelling|wording|one[- ]line|small|tiny|trivial|"
    r"constant|default\s+value|log\s+line|add\s+a\s+flag|help\s+text)\b", re.IGNORECASE)
_OVERRIDE = re.compile(r"\b(?:use|with|on)\s+(haiku|sonnet|opus|fable)\b", re.IGNORECASE)


def pick_model(task: str) -> tuple:
    """(model, reason). Explicit ask wins; hard words or a long brief or
    several files -> opus; easy words on a short single-file ask ->
    haiku; otherwise sonnet."""
    m = _OVERRIDE.search(task)
    if m:
        return m.group(1).lower(), "you asked for it"
    words = len(task.split())
    files = len(re.findall(r"[\w/.-]+\.(?:py|yaml|md|html|js|sql|toml|json)\b", task))
    if _HARD.search(task) or words > 45 or files >= 3:
        return "opus", "hard words, a long brief, or several files"
    if _EASY.search(task) and words <= 18 and files <= 1:
        return "haiku", "a small, single-file change"
    return "sonnet", "an ordinary change"


def next_rung(model: str):
    try:
        i = LADDER.index(model)
    except ValueError:
        return None
    return LADDER[i + 1] if i + 1 < len(LADDER) else None


def init(send_fn) -> None:
    global _send_fn
    _send_fn = send_fn


def available() -> bool:
    return shutil.which("claude") is not None and (REPO / ".git").exists()


# ---------------------------------------------------------------- jobs --

def _load() -> list:
    try:
        return json.loads(JOBS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save(jobs: list) -> None:
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(JOBS_PATH, json.dumps(jobs, indent=2, ensure_ascii=False))


def _update(job_id: str, **fields) -> dict:
    with locked(JOBS_PATH):
        jobs = _load()
        for job in jobs:
            if job["id"] == job_id:
                job.update(fields)
                _save(jobs)
                return job
    raise ToolError(f"no coding job {job_id!r}")


def _slug(task: str) -> str:
    words = re.findall(r"[a-z0-9]+", task.lower())[:5]
    return "-".join(words) or "task"


def prompt_for(task: str, branch: str) -> str:
    return (
        f"You are working in a git WORKTREE of the Kyraan repo, on branch {branch}. "
        "Kyraan is a self-hosted personal AI Telegram assistant (Python). Read "
        "CLAUDE.md and docs/ if present before changing anything.\n\n"
        f"TASK from the owner: {task}\n\n"
        "Rules: change only what the task needs; keep the existing style and the "
        "commit-message-as-narrative culture in mind but DO NOT commit or push — the "
        "owner reviews the branch; run the relevant tests with "
        "`.venv/bin/python -m pytest -q <files>` (and the full suite if the change "
        "is broad) and make them pass; never touch files outside this worktree; "
        "never read .env. Finish with a SHORT summary (under 120 words): what you "
        "changed, which tests you ran and their result, and anything the owner "
        "must decide."
    )


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
    env["PATH"] = ":".join(p for p in [
        "/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "")] if p)
    return env


def _git(args: list, cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                         text=True, timeout=120)
    if out.returncode != 0:
        raise ToolError(f"git {' '.join(args[:2])} failed: {(out.stderr or out.stdout).strip()[:200]}")
    return out.stdout


def start(chat_id: int, task: str) -> dict:
    """Create the worktree + branch and launch the background job.
    Returns the job record. One job at a time."""
    task = " ".join(str(task or "").split())
    if len(task) < 8:
        raise ToolError("describe the coding task in a sentence or two")
    if not available():
        raise ToolError("Claude Code (`claude`) is not installed on this machine, or the repo is not a git checkout")
    jobs = _load()
    if any(j["status"] == "running" for j in jobs):
        raise ToolError("a coding task is already running — say \"code status\"")
    stamp = datetime.now(timezone.utc).strftime("%m%d-%H%M")
    job_id = f"{_slug(task)}-{stamp}"
    branch = f"kyraan-agent/{job_id}"
    workdir = WORKTREES / job_id
    WORKTREES.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-b", branch, str(workdir), "main"], REPO)
    venv = REPO / ".venv"
    if venv.exists() and not (workdir / ".venv").exists():
        os.symlink(venv, workdir / ".venv")
        # per-worktree exclude, so the agent's `git status` is clean
        # (the smoke run flagged the symlink as untracked)
        try:
            exclude = Path(_git(["rev-parse", "--git-path", "info/exclude"], workdir).strip())
            exclude = exclude if exclude.is_absolute() else workdir / exclude
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with open(exclude, "a") as fh:
                fh.write("\n.venv\n")
        except Exception as exc:
            log_event("code_task_exclude_failed", error=str(exc)[:100])
    model, why = pick_model(task)
    job = {"id": job_id, "chat_id": chat_id, "task": task, "branch": branch,
           "dir": str(workdir), "status": "running", "model": model, "model_why": why,
           "escalated_from": "",
           "started": datetime.now(timezone.utc).isoformat(), "finished": "",
           "summary": "", "diffstat": "", "cost_usd": 0.0, "turns": 0, "error": ""}
    with locked(JOBS_PATH):
        _save(_load() + [job])
    log_event("code_task_started", job=job_id, task=task[:120], branch=branch,
              model=model, why=why)
    loop = asyncio.get_event_loop()
    _running[job_id] = loop.create_task(_run(job))
    return job


def _run_claude(job: dict) -> dict:
    cmd = ["claude", "-p", prompt_for(job["task"], job["branch"]),
           "--output-format", "json", "--max-turns", str(MAX_TURNS),
           "--model", job.get("model") or "sonnet",
           "--allowedTools", *ALLOWED_TOOLS]
    out = subprocess.run(cmd, cwd=job["dir"], env=_clean_env(),
                         capture_output=True, text=True, timeout=TIMEOUT_S)
    try:
        data = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {}
    return {"rc": out.returncode, "data": data,
            "stderr": (out.stderr or "")[-400:], "raw": (out.stdout or "")[-400:]}


def _looks_unfinished(summary: str, data: dict) -> bool:
    """The agent stopped short: hit the turn cap, or says so itself."""
    if int(data.get("num_turns") or 0) >= MAX_TURNS:
        return True
    return bool(re.search(r"\b(?:could not|couldn'?t|unable to|not able to|ran out of|"
                          r"gave up|too complex|beyond)\b", summary, re.IGNORECASE))


async def _run(job: dict) -> None:
    job_id = job["id"]
    try:
        res = await asyncio.to_thread(_run_claude, job)
        data = res["data"]
        summary = str(data.get("result") or "").strip()
        failed = res["rc"] != 0 or bool(data.get("is_error"))
        spent = float(data.get("total_cost_usd") or 0)
        higher = next_rung(job.get("model") or "sonnet")
        if (failed or _looks_unfinished(summary, data)) and higher and not job.get("escalated_from"):
            # one rung up, same worktree: the stronger model continues
            log_event("code_task_escalated", job=job_id, frm=job.get("model"), to=higher)
            job = _update(job_id, escalated_from=job.get("model"), model=higher,
                          cost_usd=round(float(job.get("cost_usd") or 0) + spent, 4))
            job["task"] = job["task"] + (
                " (A previous attempt by a smaller model stopped short; its partial "
                "edits, if any, are in this worktree — review them, then finish the task.)")
            res = await asyncio.to_thread(_run_claude, job)
            data = res["data"]
            summary = str(data.get("result") or "").strip()
            failed = res["rc"] != 0 or bool(data.get("is_error"))
            spent = float(job.get("cost_usd") or 0) + float(data.get("total_cost_usd") or 0)
        if failed:
            err = summary or res["stderr"] or res["raw"] or f"exit {res['rc']}"
            job = _update(job_id, status="failed", error=err[:600],
                          finished=datetime.now(timezone.utc).isoformat(),
                          cost_usd=round(spent, 4),
                          turns=int(data.get("num_turns") or 0))
        else:
            diffstat = _git(["diff", "--stat", "main"], Path(job["dir"])).strip()
            job = _update(job_id, status="done", summary=summary[:2000], diffstat=diffstat[:1500],
                          finished=datetime.now(timezone.utc).isoformat(),
                          cost_usd=round(spent, 4),
                          turns=int(data.get("num_turns") or 0))
    except subprocess.TimeoutExpired:
        job = _update(job_id, status="failed", error=f"timed out after {TIMEOUT_S // 60} minutes",
                      finished=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        job = _update(job_id, status="failed", error=str(exc)[:400],
                      finished=datetime.now(timezone.utc).isoformat())
    finally:
        _running.pop(job_id, None)
    log_event("code_task_finished", job=job_id, status=job["status"],
              cost_usd=job.get("cost_usd"), turns=job.get("turns"))
    if _send_fn is not None:
        try:
            await _send_fn(job["chat_id"], report(job))
        except Exception as exc:
            log_event("code_task_report_failed", job=job_id, error=str(exc)[:120])


def report(job: dict) -> str:
    head = f"🛠 Coding task {job['status']}: {job['task'][:100]}"
    if job["status"] == "failed":
        return (f"{head}\nModel: {job.get('model', '?')}"
                + (f" (escalated from {job['escalated_from']})" if job.get("escalated_from") else "")
                + f"\nBranch {job['branch']} kept for inspection.\nError: {job['error']}")
    lines = [head, "", job["summary"] or "(no summary)"]
    if job["diffstat"]:
        lines += ["", "Changed:", job["diffstat"]]
    else:
        lines += ["", "Changed: nothing (no diff against main)"]
    model_line = (f"{job.get('model', '?')}" + (f" (escalated from {job['escalated_from']})"
                                                 if job.get("escalated_from") else
                                                 f" — {job.get('model_why', '')}"))
    lines += ["", f"Model: {model_line}",
              f"Branch {job['branch']} — ${job['cost_usd']:.2f}, {job['turns']} turns.",
              'Say "code diff" to read it, "code discard" to drop it; merge it yourself when happy.']
    return "\n".join(lines)


def status() -> dict:
    jobs = _load()
    if not jobs:
        return {"jobs": 0, "note": "no coding tasks yet"}
    last = jobs[-1]
    return {"jobs": len(jobs), "last": {k: last.get(k) for k in
            ("id", "task", "status", "branch", "started", "finished", "cost_usd", "model")}}


def diff(job_id: str = "", max_chars: int = 3500) -> dict:
    jobs = _load()
    job = next((j for j in reversed(jobs) if not job_id or j["id"].startswith(job_id)), None)
    if job is None:
        raise ToolError("no such coding task — say \"code status\"")
    if not Path(job["dir"]).exists():
        raise ToolError(f"the worktree for {job['id']} is gone (discarded?)")
    text = _git(["diff", "main"], Path(job["dir"]))
    return {"job": job["id"], "branch": job["branch"], "chars": len(text),
            "diff": text[:max_chars] + ("\n… (truncated)" if len(text) > max_chars else "")}


def discard(job_id: str = "") -> dict:
    jobs = _load()
    job = next((j for j in reversed(jobs) if not job_id or j["id"].startswith(job_id)), None)
    if job is None:
        raise ToolError("no such coding task")
    if job["status"] == "running":
        raise ToolError("that task is still running — wait for its report")
    if Path(job["dir"]).exists():
        _git(["worktree", "remove", "--force", job["dir"]], REPO)
    try:
        _git(["branch", "-D", job["branch"]], REPO)
    except ToolError:
        pass
    _update(job["id"], status="discarded")
    log_event("code_task_discarded", job=job["id"])
    return {"discarded": job["id"], "branch": job["branch"]}


async def call(tool_name: str, args: dict) -> object:
    if tool_name == "code.task":
        return start(int(args.get("chat_id") or 0), str(args.get("task", "")))
    if tool_name == "code.status":
        return await asyncio.to_thread(status)
    if tool_name == "code.diff":
        return await asyncio.to_thread(diff, str(args.get("job", "") or ""))
    if tool_name == "code.discard":
        return await asyncio.to_thread(discard, str(args.get("job", "") or ""))
    raise ToolError(f"code_agent does not provide {tool_name!r}")
