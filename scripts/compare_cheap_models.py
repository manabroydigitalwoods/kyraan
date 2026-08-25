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

from openai import OpenAI

from kyraan.intent.normalize import _CONTEXT_SECTION, _SYSTEM_PROMPT

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
    client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
    right = 0
    bad_json = 0
    started = time.monotonic()
    rows = []
    for text, history, accepted in CASES:
        system = _SYSTEM_PROMPT + (_CONTEXT_SECTION.format(history=history) if history else "")
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": text}],
            max_tokens=1024,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
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
