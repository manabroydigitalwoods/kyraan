"""One-shot intelligent classification of the existing memory index.

Migrates the MD tree into the engine index if needed, then asks the
frontier model to classify every active fact (kind/era/sphere/term/
importance/flags) and to FLAG — never delete — junk, duplicates, and
contradictions. Metadata updates apply automatically (they only change
ranking); anything destructive is a printed report for the owner.

Usage: .venv/bin/python scripts/classify_memory.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from kyraan.memory import engine  # noqa: E402
from kyraan.model_router import router  # noqa: E402

_SYSTEM = """You classify a personal assistant's stored facts about its
owner (the entries themselves carry the personal context).
For EVERY entry return:
- kind: identity|relationship|preference|routine|work|situational|other
- era: current|past   - sphere: personal|work|both   - term: long|short
- importance: critical (emergency-relevant: allergies, medical, emergency
  contacts) | high (core identity: family names, birthdays) | normal
- flags: subset of [health,safety,emergency,danger,fun,sentimental,
  milestone,emotional,sensitive]
- junk: true when the entry is not a personal fact at all (a reminder
  mistaken for a fact, a public figure, garbage)
- duplicate_of: the id of another entry with the same meaning, else null
- conflicts_with: ids of entries this contradicts (e.g. two different
  names for the same person), else []
Respond ONLY JSON: {"entries": [{"id": "...", ...}]}"""


def main() -> None:
    engine.migrate_from_tree()
    entries = engine.active_entries()
    if not entries:
        print("Index is empty.")
        return
    listing = json.dumps(
        [{"id": e["id"], "content": e["content"], "target": e["target"]} for e in entries],
        ensure_ascii=False, indent=1)
    response = router.call(prompt=listing, system=_SYSTEM, tier="frontier",
                           force_json=True, max_tokens=4096)
    verdicts = {v["id"]: v for v in json.loads(router.strip_code_fence(response.text)).get("entries", [])}

    index = engine._load()
    updated = 0
    for entry in index:
        verdict = verdicts.get(entry["id"])
        if not verdict or not entry["active"]:
            continue
        for field, valid in (("kind", engine._VALID_KINDS), ("era", engine._VALID_ERA),
                             ("sphere", engine._VALID_SPHERE), ("term", engine._VALID_TERM),
                             ("importance", engine._VALID_IMPORTANCE)):
            value = verdict.get(field)
            if value in valid and entry.get(field) != value:
                entry[field] = value
                updated += 1
        flags = sorted(set(verdict.get("flags") or []) & engine._VALID_FLAGS)
        if flags != entry.get("flags"):
            entry["flags"] = flags
            updated += 1
    engine._save(index)
    print(f"Metadata updated on {updated} fields across {len(entries)} facts.\n")

    by_id = {e["id"]: e["content"] for e in entries}
    print("=== OWNER REPORT — nothing below was auto-changed ===")
    for entry_id, verdict in verdicts.items():
        content = by_id.get(entry_id, "?")
        if verdict.get("junk"):
            print(f"JUNK?       [{entry_id}] {content}")
        if verdict.get("duplicate_of"):
            print(f"DUPLICATE   [{entry_id}] {content}\n"
                  f"        of  [{verdict['duplicate_of']}] {by_id.get(verdict['duplicate_of'], '?')}")
        for other in verdict.get("conflicts_with") or []:
            print(f"CONFLICT    [{entry_id}] {content}\n"
                  f"       vs   [{other}] {by_id.get(other, '?')}")


if __name__ == "__main__":
    main()
