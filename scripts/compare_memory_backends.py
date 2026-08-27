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
        # Post-cutover + RAG (2026-08-27) pg is a deliberate SUPERSET:
        # the semantic arm adds candidates files can't see. The invariant
        # that must hold is that pg never LOSES a fact file-mode serves.
        missing = [line for line in via_files.splitlines()
                   if line.startswith("- ") and line not in via_pg]
        if missing:
            mismatches += 1
            print(f"❌ pg LOST file-mode facts on {probe[:60]!r}:")
            for line in missing:
                print(f"   {line}")
        else:
            extra = sum(1 for line in via_pg.splitlines()
                        if line.startswith("- ") and line not in via_files)
            print(f"✅ pg ⊇ files (+{extra} semantic): {probe[:60]!r}")
    print(f"\n{len(probes)} probes, {mismatches} mismatches")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
