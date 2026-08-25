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


def test_confirm_keyboard_only_when_a_confirmation_is_pending():
    from kyraan.agents import orchestrator

    orchestrator._pending_confirmations.pop(77, None)
    assert telegram_bot._confirm_keyboard(77) is None

    orchestrator._pending_confirmations[77] = ("call", "handler", 0.0)
    try:
        kb = telegram_bot._confirm_keyboard(77)
        buttons = kb.inline_keyboard[0]
        assert [b.callback_data for b in buttons] == ["kyraan_yes", "kyraan_no"]
    finally:
        orchestrator._pending_confirmations.pop(77, None)


async def test_burst_messages_combine_into_one_answer(monkeypatch):
    """The owner sends thoughts as several quick messages — they must be
    answered as ONE combined message (like this harness itself batches
    mid-turn messages), not as serial fragments."""
    from types import SimpleNamespace
    from kyraan.agents import orchestrator

    handled = []

    async def fake_handle(chat_id, text):
        handled.append(text)
        return "combined answer"

    monkeypatch.setattr(orchestrator, "handle_message", fake_handle)
    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    monkeypatch.setattr(telegram_bot, "_BURST_WINDOW_S", 0.05)

    replies = []

    def make_update(text):
        async def reply_text(reply, reply_markup=None, do_quote=False):
            replies.append(reply)
        message = SimpleNamespace(text=text, reply_text=reply_text)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=9),
            message=message,
        )

    class FakeBot:
        async def send_chat_action(self, chat_id, action):
            pass

    ctx = SimpleNamespace(bot=FakeBot())
    tasks = [
        asyncio.get_event_loop().create_task(telegram_bot._on_message(make_update(t), ctx))
        for t in ["but this is normal", "I can send message", "like this"]
    ]
    await asyncio.gather(*tasks)

    assert handled == ["but this is normal\nI can send message\nlike this"]
    assert replies == ["combined answer"]  # one reply for the whole burst
