"""Entrypoint: load config, start the Telegram channel.

Phase 1 has exactly one channel and one orchestrator — no agent router yet.
"""
from dotenv import load_dotenv

load_dotenv()   # BEFORE the import: modules read KYRAAN_* at import time

from kyraan.channels import telegram_bot  # noqa: E402


def main() -> None:
    telegram_bot.run()


if __name__ == "__main__":
    main()
