"""P3.3b backfill: replay the whole chat.jsonl history into the episode
table — cloud_text twins only, local tagging, local embedding.
Idempotent (deterministic episode ids): safe to re-run any time.

    .venv/bin/python scripts/backfill_episodes.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.control_plane import logging_setup  # noqa: E402
from kyraan.control_plane.dnd import local_now  # noqa: E402
from kyraan.store import episodes, pg  # noqa: E402


def main() -> int:
    path = logging_setup.CHAT_LOG
    parsed = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    tz = local_now().tzinfo
    days = sorted({datetime.fromisoformat(e["ts"]).astimezone(tz).date().isoformat()
                   for e in parsed if e.get("ts")})
    print(f"{len(parsed)} records across {len(days)} days")
    total = 0
    for day in days:
        result = episodes.ingest_day(day, episodes.records_for_day(day, parsed))
        total += result.get("episodes", 0)
        print(f"  {day}: {result}")
    with pg.connection() as conn:
        rows, flagged = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE flags <> '{}') FROM episode"
        ).fetchone()
        sample = conn.execute(
            """SELECT day, chat_id, flags, left(text, 110) FROM episode
               ORDER BY random() LIMIT 3""").fetchall()
    print(f"\nepisode table: {rows} rows ({flagged} carrying sensitivity flags); "
          f"this run touched {total}")
    print("spot-check:")
    for day, chat_id, flags, snippet in sample:
        print(f"  [{day} chat {chat_id} {flags}] {snippet!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
