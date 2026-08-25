"""Head-to-head intent-classification benchmark for cheap-tier candidates.

Runs Kyraan's REAL test set — the classic 14-case classifier set plus every
live misclassification from the 2026-08-25/26 sessions — against local
Ollama models, using the production system prompt (and context section for
the continuation cases). Scores intent accuracy and JSON validity.

Usage:  .venv/bin/python scripts/compare_cheap_models.py llama3.1:8b qwen3:8b
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import urllib.request

from kyraan.intent.normalize import _CONTEXT_SECTION, _SYSTEM_PROMPT


def ollama_chat(model: str, system: str, prompt: str, max_tokens: int = 1024) -> str:
    """Native /api/chat — the OpenAI-compat endpoint ignores think:false
    and Qwen3 then burns 8-12s of hidden reasoning per call."""
    payload = {
        "model": model, "stream": False, "format": "json",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "options": {"num_predict": max_tokens},
    }
    if "qwen" in model:
        payload["think"] = False
    req = urllib.request.Request("http://localhost:11434/api/chat",
                                 json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)["message"].get("content", "")

_HELP_HISTORY = (
    "user: I need help\n"
    "assistant: Of course! What do you need assistance with, Manab?"
)
_SILIGURI_HISTORY = (
    "user: today moring I have to go to siliguri\n"
    "assistant: Got it — a Nagpur trip this morning."
)
_TIME_FAILED_HISTORY = (
    "user: remind me to call mom\n"
    "assistant: I couldn't work out a time for that reminder — try "
    "rephrasing with a clearer date/time."
)

# (message, history, accepted intents) — a set because some readings are
# legitimately ambiguous.
CASES = [
    ("set reminder in 5mis 'Call to RUma'", "", {"reminders.create"}),
    ("wat tym is it", "", {"qa.answer"}),
    ("who are you?", "", {"qa.answer"}),
    ("do I have any reminders?", "", {"reminders.list"}),
    ("cancel my reminder", "", {"reminders.cancel"}),
    ("any meetings tomorrow?", "", {"calendar.list"}),
    ("add a meeting with suman tomorrow 5pm to my calendar", "", {"calendar.create"}),
    ("any new emails?", "", {"email.check"}),
    ("is the AC on?", "", {"home.query"}),
    ("turn off the AC", "", {"home.control"}),
    ("let me fix you", "", {"qa.answer"}),
    ("remember that my son's school starts at 8am", "", {"qa.answer"}),
    ("tomorrow morning", "", {"incomplete"}),
    ("how long has the AC been on?", "", {"home.query"}),
    # Live failures, with the conversation context production would have:
    ("on my smoke havite", _HELP_HISTORY, {"qa.answer"}),
    ("to buy something", _SILIGURI_HISTORY, {"qa.answer"}),
    ("6pm", _TIME_FAILED_HISTORY, {"reminders.create"}),
    ("i'm smoking right now", "", {"qa.answer"}),
]


def run(model: str) -> None:
    right = 0
    bad_json = 0
    started = time.monotonic()
    rows = []
    for text, history, accepted in CASES:
        system = _SYSTEM_PROMPT + (_CONTEXT_SECTION.format(history=history) if history else "")
        raw = ollama_chat(model, system, text)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
        try:
            intent = json.loads(raw).get("intent")
        except json.JSONDecodeError:
            bad_json += 1
            intent = "<bad json>"
        ok = intent in accepted
        right += ok
        rows.append(f"  {'✓' if ok else '✗'} {text[:44]:<46} -> {intent}"
                    + ("" if ok else f"  (wanted {'/'.join(sorted(accepted))})"))
    elapsed = time.monotonic() - started
    print(f"\n{model}: {right}/{len(CASES)} correct, {bad_json} bad-JSON, "
          f"{elapsed / len(CASES):.1f}s/case")
    print("\n".join(rows))


if __name__ == "__main__":
    models = sys.argv[1:] or ["llama3.1:8b"]
    for m in models:
        run(m)
