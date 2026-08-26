"""Prompt quality report — deterministic analysis, never an editor.

    .venv/bin/python scripts/prompt_report.py

Measures the REAL assembled agent prompt (section sizes), checks cache
health from today's model_call events (the static prefix earns a ~90%
input discount only while it stays byte-stable), flags rules referencing
tools that don't exist, and lists near-duplicate sentences across
sections. Findings are printed for the owner — prompt edits stay human,
gated by scripts/eval.py, per plan §6 (nothing self-modifies without a
human-reviewable layer).
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from kyraan.agents import agent_loop  # noqa: E402
from kyraan.agents.capabilities import capability_brief  # noqa: E402


def toks(text: str) -> int:
    """Rough token estimate (chars/4) — right order of magnitude, and the
    section RATIOS are what matter here."""
    return max(1, len(text) // 4)


def _sentences(text: str) -> list:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text)
            if len(s.strip()) > 60]


def _norm_words(s: str) -> set:
    return {w for w in re.findall(r"[a-z]{3,}", s.lower())}


def main() -> None:
    sections = {
        "capability brief": capability_brief(),
        "tool menu": agent_loop._tools_block(),
        "doctrine + style (static)": agent_loop._AGENT_SYSTEM.format(
            capabilities="", tools=""),
    }
    try:
        from kyraan.memory import engine
        sections["memory block (current)"] = engine.memory_context("")
    except Exception as exc:
        print(f"(memory block unavailable: {exc})")

    print("== Section sizes (approx tokens) ==")
    total = sum(toks(t) for t in sections.values())
    for name, text in sorted(sections.items(), key=lambda kv: -toks(kv[1])):
        t = toks(text)
        print(f"  {t:>6}  {t * 100 // total:>3}%  {name}")
    print(f"  {total:>6}  100%  total static+context (history/message ride on top)\n")

    # --- cache health from today's events --------------------------------
    today = datetime.now(timezone.utc).date().isoformat()
    calls = []
    events_path = ROOT / "logs" / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (e.get("kind") == "model_call" and e.get("ts", "").startswith(today)
                    and e.get("provider") == "openai"):
                calls.append(e)
    print("== Prompt-cache health (today's frontier calls) ==")
    if not calls:
        print("  no frontier calls logged today\n")
    else:
        eligible = [c for c in calls if int(c.get("input_tokens") or 0) >= 1024]
        misses = [c for c in eligible if int(c.get("cached_tokens") or 0) == 0]
        ratios = [int(c.get("cached_tokens") or 0) / int(c.get("input_tokens") or 1)
                  for c in eligible]
        avg = sum(ratios) / len(ratios) * 100 if ratios else 0
        print(f"  {len(calls)} calls, {len(eligible)} cache-eligible (>=1024 input tokens)")
        print(f"  average cached-input share: {avg:.0f}%  "
              f"(each cached token bills at ~10%)")
        print(f"  full cache misses: {len(misses)}"
              + ("  <- PREFIX INSTABILITY: something dynamic sits in the "
                 "system prompt" if len(misses) > len(eligible) * 0.2 else ""))
        lat = sorted(int(c.get("latency_ms") or 0) for c in calls)
        print(f"  latency p50={lat[len(lat)//2]}ms p95={lat[int(len(lat)*0.95)]}ms\n")

    # --- dead tool references --------------------------------------------
    known = set(agent_loop.TOOLS)
    text_all = " ".join(sections.values())
    mentioned = set(re.findall(r"\b([a-z_]+\.[a-z_]+)\b", text_all))
    suspicious = {m for m in mentioned
                  if m.split(".")[0] in {t.split(".")[0] for t in known}
                  and m not in known}
    print("== Tool references ==")
    if suspicious:
        for m in sorted(suspicious):
            print(f"  DEAD? {m!r} mentioned in prompt but not a registered tool")
    else:
        print("  every tool-like reference resolves to a registered tool")
    print()

    # --- near-duplicate rules across sections ----------------------------
    print("== Near-duplicate sentences across sections ==")
    found = 0
    items = []
    for name, text in sections.items():
        for s in _sentences(text):
            items.append((name, s, _norm_words(s)))
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a[0] == b[0] or not a[2] or not b[2]:
                continue
            overlap = len(a[2] & b[2]) / len(a[2] | b[2])
            if overlap >= 0.6:
                found += 1
                print(f"  {overlap:.0%} overlap between [{a[0]}] and [{b[0]}]:")
                print(f"     A: {a[1][:110]}")
                print(f"     B: {b[1][:110]}")
    if not found:
        print("  none above the 60% threshold")
    print("\nReport only — edits stay human, and any accepted change must "
          "pass scripts/eval.py before deploy.")


if __name__ == "__main__":
    sys.exit(main())
