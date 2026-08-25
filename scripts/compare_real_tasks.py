"""Real-task output comparison for cheap-tier candidates: reminder
extraction and burst planning, on the ACTUAL cases from the 2026-08-25/26
live sessions, using the production prompts. Prints each model's raw
output with pass/fail validation.

Usage:  .venv/bin/python scripts/compare_real_tasks.py llama3.1:8b qwen3:8b
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from compare_cheap_models import ollama_chat  # noqa: E402  (same dir)

from kyraan.agents.orchestrator import _BURST_PLAN_SYSTEM, _EXTRACT_WHEN_SYSTEM  # noqa: E402
from kyraan.control_plane.dnd import local_now  # noqa: E402
from kyraan.triggers.scheduler import _parse_when  # noqa: E402

# (text, validator) — validators return "" when good, else the problem.
NOW = local_now()


def _future(data):
    when = _parse_when(data["when_iso"])
    return "" if when > NOW else f"PAST time {data['when_iso']}"


def _clock(hour):
    def check(data):
        when = _parse_when(data["when_iso"])
        return "" if when.hour == hour else f"wrong clock {when.hour}:00 (wanted {hour}:00)"
    return check


EXTRACTIONS = [
    # llama live-failed this exact shape as a PAST time (walkthrough v3):
    ("remind me in 45 mins to check the geyser", _future),
    # the live "8pm became 20:49" and "9pm became 8:00 AM" family:
    ("remind me at 8pm to call Suman", _clock(20)),
    ("wake me tomorrow at 7am", _clock(7)),
]

BURSTS = [
    ["hey hi", "how are you?", "let cehck tomorrow email", "lety me kow", "what is plan"],
    ["today moring I have to go to siliguri", "to buy something", "very important"],
    ["is the AC on?", "any new emails?"],
]


def run(model: str) -> None:
    print(f"\n================ {model} ================")
    print("--- reminder extraction ---")
    for text, validate in EXTRACTIONS:
        raw = ollama_chat(model, _EXTRACT_WHEN_SYSTEM.format(now=NOW.isoformat()), text)
        try:
            data = json.loads(raw)
            problem = validate(data)
            verdict = "✓" if not problem else f"✗ {problem}"
            print(f"  {verdict}  {text!r}\n      -> {json.dumps(data)}")
        except Exception as exc:
            print(f"  ✗ {text!r}\n      -> UNPARSEABLE {raw[:120]!r} ({exc})")

    print("--- burst planning ---")
    for texts in BURSTS:
        numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(texts))
        raw = ollama_chat(model, _BURST_PLAN_SYSTEM.format(n=len(texts), numbered=numbered),
                          "Plan the requests.")
        try:
            requests = json.loads(raw).get("requests")
            ok = isinstance(requests, list) and 0 < len(requests) <= len(texts)
            print(f"  {'✓' if ok else '✗'}  {texts}\n      -> {requests}")
        except Exception as exc:
            print(f"  ✗  {texts}\n      -> UNPARSEABLE {raw[:120]!r} ({exc})")


if __name__ == "__main__":
    for m in sys.argv[1:] or ["llama3.1:8b", "qwen3:8b"]:
        run(m)
