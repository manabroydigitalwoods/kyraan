"""P3.2b parity harness: memory_context() under files vs pg backends over
the eval prompts plus the 20 most recent real user messages. The
Done-when is byte-identical output on every probe.

    .venv/bin/python scripts/compare_memory_backends.py
"""
import difflib
import importlib.util
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.memory import engine  # noqa: E402


def _eval_prompts() -> list:
    spec = importlib.util.spec_from_file_location("kyraan_eval", REPO / "scripts" / "eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return [c.msg for c in mod.CASES]


def _recent_user_messages(n: int = 20) -> list:
    chat = REPO / "logs" / "chat.jsonl"
    if not chat.exists():
        return []
    msgs = []
    for line in chat.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("role") == "user" and rec.get("text"):
            msgs.append(rec["text"])
    return msgs[-n:]


def main() -> int:
    probes = ["", *_eval_prompts(), *_recent_user_messages()]
    mismatches = 0
    for probe in probes:
        os.environ["KYRAAN_MEMORY_BACKEND"] = "files"
        via_files = engine.memory_context(probe)
        os.environ["KYRAAN_MEMORY_BACKEND"] = "pg"
        via_pg = engine.memory_context(probe)
        os.environ.pop("KYRAAN_MEMORY_BACKEND", None)
        if via_files != via_pg:
            mismatches += 1
            print(f"❌ DIFF on probe: {probe[:70]!r}")
            for line in difflib.unified_diff(via_files.splitlines(),
                                             via_pg.splitlines(),
                                             "files", "pg", lineterm=""):
                print(f"   {line}")
        else:
            print(f"✅ identical ({len(via_files):4d} chars): {probe[:60]!r}")
    print(f"\n{len(probes)} probes, {mismatches} mismatches")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
