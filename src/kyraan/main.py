"""Entrypoint: load config, start the Telegram channel.

Phase 1 has exactly one channel and one orchestrator — no agent router yet.
"""
from dotenv import load_dotenv

from kyraan.channels import telegram_bot


def main() -> None:
    load_dotenv()
    telegram_bot.run()


if __name__ == "__main__":
    main()
