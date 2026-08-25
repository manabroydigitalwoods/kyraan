"""Channel-side behavior that doesn't need a live bot: the typing loop."""
import asyncio

import pytest

from kyraan.channels import telegram_bot


async def test_typing_loop_repeats_and_cancels_cleanly(monkeypatch):
    actions = []

    class FakeBot:
        async def send_chat_action(self, chat_id, action):
            actions.append((chat_id, str(action)))

    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        await real_sleep(0)

    monkeypatch.setattr(telegram_bot.asyncio, "sleep", fast_sleep)
    task = asyncio.get_event_loop().create_task(telegram_bot._typing_loop(FakeBot(), 42))
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(actions) >= 2  # re-sent, not one-shot
    assert actions[0][0] == 42 and "typing" in actions[0][1].lower()
