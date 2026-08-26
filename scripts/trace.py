"""Pretty-print one turn's complete flow — user message, every model
call, every tool call with duration, the final reply, total wall time.

    .venv/bin/python scripts/trace.py            # the most recent turn
    .venv/bin/python scripts/trace.py <turn_id>  # a specific turn (prefix ok)
    .venv/bin/python scripts/trace.py --full     # include full prompt text

Reads logs/traces.jsonl + logs/events.jsonl (current files only — rotated
archives are for manual forensics).
"""
import json
import sys
from pathlib import Path

LOGS = Path(__file__).resolve().parents[1] / "logs"


def _read(path: Path) -> list:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _clip(text: str, n: int) -> str:
    text = str(text).replace("\n", " ¶ ")
    return text if len(text) <= n else text[:n] + f"… (+{len(text) - n} chars)"


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--full"]
    full = "--full" in sys.argv
    traces = _read(LOGS / "traces.jsonl")
    events = _read(LOGS / "events.jsonl")

    wanted = args[0] if args else None
    if wanted is None:
        # Prefer a full chat turn; fall back to any traced flow (scheduled
        # runs and dev probes enter via the agent loop, no turn_start).
        with_id = [t for t in traces if t.get("turn_id")]
        starts = [t for t in with_id if t.get("kind") == "turn_start"]
        if not with_id:
            sys.exit("no turns in logs/traces.jsonl yet")
        wanted = (starts or with_id)[-1]["turn_id"]

    turn_traces = [t for t in traces if str(t.get("turn_id", "")).startswith(wanted)]
    turn_events = [e for e in events if str(e.get("turn_id", "")).startswith(wanted)]
    if not turn_traces and not turn_events:
        sys.exit(f"no records for turn {wanted!r}")

    rows = sorted(turn_traces + turn_events, key=lambda r: r.get("ts", ""))
    print(f"turn {rows[0].get('turn_id')} — {len(turn_events)} events, "
          f"{len(turn_traces)} trace records\n")
    for r in rows:
        ts = r.get("ts", "")[11:23]
        kind = r.get("kind", "?")
        if kind == "turn_start":
            print(f"{ts}  USER      {_clip(r.get('user_text', ''), 200)}")
        elif kind == "model_io":
            print(f"{ts}  MODEL     {r.get('provider')}/{r.get('model')} "
                  f"{r.get('latency_ms')}ms in={r.get('input_tokens')} "
                  f"cached={r.get('cached_tokens')}")
            if full:
                print(f"            SYSTEM: {_clip(r.get('system', ''), 100000)}")
                print(f"            PROMPT: {_clip(r.get('prompt', ''), 100000)}")
            print(f"            -> {_clip(r.get('response', ''), 300)}")
        elif kind == "tool_call" and r.get("tool"):
            print(f"{ts}  TOOL      {r.get('tool')} {_clip(json.dumps(r.get('args', {})), 140)}")
        elif kind == "tool_result":
            status = "ok" if r.get("ok") else f"FAILED: {_clip(r.get('error', ''), 120)}"
            dur = f" {r.get('duration_ms')}ms" if r.get("duration_ms") is not None else ""
            print(f"{ts}    result  {r.get('tool') or r.get('skill')}{dur} {status}")
        elif kind == "stage":
            print(f"{ts}    stage   {r.get('stage'):<22} {r.get('ms')}ms")
        elif kind == "turn_end":
            print(f"{ts}  REPLY     ({r.get('total_ms')}ms total) {_clip(r.get('reply', ''), 300)}")
            stages = r.get("stages") or []
            if stages and r.get("total_ms"):
                total = r["total_ms"]
                print("            —— timing ——")
                for s in sorted(stages, key=lambda x: -x.get("ms", 0)):
                    pct = s.get("ms", 0) * 100 // max(total, 1)
                    extra = s.get("provider") or ""
                    print(f"            {s.get('ms', 0):>7}ms {pct:>3}%  {s.get('stage')} {extra}")
                accounted = sum(s.get("ms", 0) for s in stages)
                print(f"            {max(total - accounted, 0):>7}ms {max(total - accounted, 0) * 100 // max(total, 1):>3}%  (transport/overhead)")
        elif kind in ("model_call", "tool_retry"):
            continue  # detail already shown via model_io / final result
        else:
            extra = {k: v for k, v in r.items()
                     if k not in ("ts", "kind", "turn_id")}
            print(f"{ts}  {kind:<9} {_clip(json.dumps(extra, default=str), 160)}")


if __name__ == "__main__":
    main()
