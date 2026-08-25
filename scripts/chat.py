"""Dev-only local harness: exercises orchestrator.handle_message without a
Telegram bot, so intent normalization / reminders / Q&A can be tested
against the real configured model provider before Telegram creds exist.

Not part of the installed package — run directly: `python scripts/chat.py`

Limitation: reminders scheduled here use plain asyncio tasks, not the real
JobQueue, so `cancel_reminder` only removes the persisted record — an
already-scheduled asyncio task will still fire. Fine for a dev harness,
not for production use.
"""
import asyncio
from datetime import datetime

from dotenv import load_dotenv

from kyraan.agents import orchestrator
from kyraan.triggers import scheduler

CHAT_ID = 0


def schedule_fn(job_name: str, run_at: datetime, payload: dict) -> None:
    delay = max((run_at - datetime.now().astimezone()).total_seconds(), 0)

    async def fire_later():
        await asyncio.sleep(delay)
        await scheduler.fire(payload["reminder_id"], payload["chat_id"], payload["text"])

    asyncio.get_event_loop().create_task(fire_later())


def cancel_fn(job_name: str) -> None:
    pass  # best-effort no-op — see module docstring


async def send_fn(chat_id: int, text: str) -> None:
    print(f"\n[proactive] {text}")


async def main() -> None:
    load_dotenv()
    scheduler.init(schedule_fn=schedule_fn, cancel_fn=cancel_fn, send_fn=send_fn)
    print("Kyraan local CLI — real model calls, no Telegram needed. Ctrl-D to quit.\n")

    loop = asyncio.get_event_loop()
    while True:
        try:
            text = await loop.run_in_executor(None, input, "> ")
        except EOFError:
            break
        if not text.strip():
            continue
        reply = await orchestrator.handle_message(CHAT_ID, text)
        print(reply)


if __name__ == "__main__":
    asyncio.run(main())
