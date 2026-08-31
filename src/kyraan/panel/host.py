"""Host vitals for the panel — what the MacBook itself is doing.

stdlib and the OS's own tools only. psutil would be the obvious
dependency and is deliberately not taken: this reads four numbers and a
process table on one platform, and the panel's whole argument for
existing is that it adds nothing to the machine it watches.

NOTHING here writes. The sampler keeps its history in a bounded deque in
memory, which is also why the graph resets when the panel restarts —
persisting it would mean the reader starts writing, and rule 1 in
docs/design/web_panel.md says it does not.
"""
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone

# Roles worth naming. A process table is a wall of paths; what the owner
# actually wants to know is "which PART of Kyraan is eating the machine",
# so each pattern maps a command to the piece of the system it IS.
_ROLES = (
    (re.compile(r"llama-server|ollama"), "local model", "the cheap tier's brain"),
    (re.compile(r"postgres"), "postgres", "facts, episodes, triples"),
    (re.compile(r"redis-server"), "redis", "session state"),
    # OrbStack on this machine, Docker Desktop elsewhere — match both, or
    # the whole container stack shows up as an untagged mystery process.
    (re.compile(r"OrbStack|com\.docker|Docker Desktop|dockerd|qemu"),
     "containers", "searxng, home assistant"),
    (re.compile(r"kyraan\.main"), "kyraan bot", "the agent loop"),
    (re.compile(r"scripts/panel\.py"), "panel", "this page"),
    (re.compile(r"mlx|whisper"), "whisper", "local voice transcription"),
)

_PS_ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+\.\d+)\s+(\S.*)$")

_SAMPLE_SECONDS = 5
_HISTORY = deque(maxlen=720)          # ~1 hour at 5s
_sampler_started = False
_lock = threading.Lock()


def _run(args, timeout=4) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _memory() -> dict:
    """macOS 'used' is active + wired + compressed. Free pages alone are a
    famously misleading number here — the OS keeps very few."""
    out = _run(["vm_stat"])
    if not out:
        return {}
    page_match = re.search(r"page size of (\d+)", out)
    page = int(page_match.group(1)) if page_match else 4096
    pages = {}
    for line in out.splitlines()[1:]:
        name, _, value = line.partition(":")
        digits = value.strip().rstrip(".")
        if digits.isdigit():
            pages[name.strip().lower()] = int(digits)

    def by(*names):
        return sum(pages.get(n, 0) for n in names) * page

    try:
        total = int(_run(["sysctl", "-n", "hw.memsize"]).strip())
    except ValueError:
        total = 0
    used = by("pages active", "pages wired down", "pages occupied by compressor")
    return {
        "total": total, "used": used,
        "free": by("pages free", "pages inactive", "pages speculative"),
        "compressed": by("pages occupied by compressor"),
        "wired": by("pages wired down"),
        "used_pct": round(used / total * 100, 1) if total else None,
    }


def _battery() -> dict:
    out = _run(["pmset", "-g", "batt"])
    if not out:
        return {}
    percent = re.search(r"(\d+)%", out)
    return {
        "percent": int(percent.group(1)) if percent else None,
        "power": "ac" if "AC Power" in out else "battery",
        "charging": "charging" in out.lower(),
    }


def processes(limit: int = 12) -> list:
    """Top processes by memory, each tagged with the role it plays here.

    Sorted by RSS rather than CPU on purpose: on this machine the standout
    is the local model holding gigabytes resident even while idle, and a
    CPU sort would rank a browser tab above it.
    """
    # args=, not comm=: comm is the EXECUTABLE, so every Python service on
    # the machine is "Python" and the bot cannot be told from the panel.
    out = _run(["ps", "-A", "-o", "pid=,rss=,pcpu=,args="])
    # Strict: a row must BEGIN with pid, rss, cpu. Command lines can carry
    # newlines (any `python -c` with embedded ones does), and a loose
    # split happily parsed a continuation line into a fake process — which
    # then picked up whatever role its text happened to match.
    rows = []
    for line in out.splitlines():
        match = _PS_ROW.match(line)
        if not match:
            continue
        pid, rss, cpu, command = match.groups()
        rows.append({"pid": int(pid), "rss": int(rss) * 1024,
                     "cpu": float(cpu), "command": command.strip()})

    for row in rows:
        row["role"], row["role_note"] = "", ""
        for pattern, role, note in _ROLES:
            if pattern.search(row["command"]):
                row["role"], row["role_note"] = role, note
                break
        # A path is not a name — and for an interpreter the useful name is
        # what it is RUNNING, not the interpreter.
        first = row["command"].split(" ", 1)[0].rsplit("/", 1)[-1]
        script = re.search(r"(?:-m\s+([\w.]+)|([\w./]+\.py))", row["command"])
        row["name"] = ((script.group(1) or script.group(2)).rsplit("/", 1)[-1]
                       if script else first)[:40]

    rows.sort(key=lambda r: -r["rss"])
    ours = [r for r in rows if r["role"]]
    rest = [r for r in rows if not r["role"]][:max(0, limit - len(ours))]
    return (ours + rest)[:limit + len(ours)]


def snapshot() -> dict:
    """One reading of the whole host."""
    load = os.getloadavg()
    cpus = os.cpu_count() or 1
    disk = shutil.disk_usage("/")
    procs = processes()
    by_role: dict = {}
    for row in procs:
        if not row["role"]:
            continue
        bucket = by_role.setdefault(row["role"], {
            "role": row["role"], "note": row["role_note"],
            "rss": 0, "cpu": 0.0, "procs": 0})
        bucket["rss"] += row["rss"]
        bucket["cpu"] = round(bucket["cpu"] + row["cpu"], 1)
        bucket["procs"] += 1

    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "cpus": cpus,
        "load": {"1m": round(load[0], 2), "5m": round(load[1], 2),
                 "15m": round(load[2], 2),
                 # Load per core is the number that means something across
                 # machines: 1.0 is "fully committed", above is a queue.
                 "per_core": round(load[0] / cpus, 2)},
        "memory": _memory(),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free,
                 "used_pct": round(disk.used / disk.total * 100, 1)},
        "battery": _battery(),
        "processes": procs,
        "roles": sorted(by_role.values(), key=lambda r: -r["rss"]),
    }


def _sample_loop() -> None:
    while True:
        try:
            reading = snapshot()
            with _lock:
                _HISTORY.append({
                    "at": reading["at"],
                    "load": reading["load"]["per_core"],
                    "mem_pct": (reading["memory"] or {}).get("used_pct"),
                    "roles": {r["role"]: r["rss"] for r in reading["roles"]},
                })
        except Exception:
            pass          # a sampler that dies takes the graph with it
        time.sleep(_SAMPLE_SECONDS)


def ensure_sampler() -> None:
    """Start the background sampler once, lazily — a panel nobody opens
    should not be shelling out to ps every five seconds forever."""
    global _sampler_started
    with _lock:
        if _sampler_started:
            return
        _sampler_started = True
    threading.Thread(target=_sample_loop, daemon=True,
                     name="panel-host-sampler").start()


def history() -> dict:
    with _lock:
        points = list(_HISTORY)
    return {"points": points, "sample_seconds": _SAMPLE_SECONDS,
            "capacity": _HISTORY.maxlen}
