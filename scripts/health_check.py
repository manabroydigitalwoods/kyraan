"""The doctor, on demand: live component probes + the 24h anomaly
census with thresholds. Exit 0 = OK, 1 = WARN, 2 = FAIL.

    .venv/bin/python scripts/health_check.py
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.control_plane import health  # noqa: E402


def main() -> int:
    verdict, text = health.report()
    print(text)
    print(f"\nVERDICT: {verdict}")
    return {"OK": 0, "WARN": 1, "FAIL": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
