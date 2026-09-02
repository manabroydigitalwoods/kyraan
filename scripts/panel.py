"""Run the web panel — Phase A: read-only glass (docs/design/web_panel.md).

    .venv/bin/python scripts/panel.py                 # 127.0.0.1:8765
    .venv/bin/python scripts/panel.py --port 9000
    KYRAAN_PANEL_TOKEN=... .venv/bin/python scripts/panel.py   # stable URL

Prints a URL with a one-time token in it; opening that URL moves the token
into an HttpOnly cookie and redirects. Without KYRAAN_PANEL_TOKEN the token
is regenerated every start, so a stale bookmark simply stops working.

The panel only READS — the event log, the trace log, the cost ledger, and
the trigger stores. It is safe to run beside the live bot, and it holds no
lock any writer waits on.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from kyraan.panel import server  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Kyraan read-only web panel")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default loopback — keep it there "
                             "and reach the panel over Tailscale)")
    parser.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    parser.add_argument("--lan", action="store_true",
                        help="listen on every interface so a phone on the same "
                             "network can open the panel (prints the URL; the "
                             "token then travels in clear over Wi-Fi — prefer "
                             "Tailscale)")
    args = parser.parse_args()
    server.serve(host="0.0.0.0" if args.lan else args.host, port=args.port)


if __name__ == "__main__":
    main()
