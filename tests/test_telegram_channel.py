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

    async def fake_burst(chat_id, texts, superseded=None):
        handled.append(texts)
        return [(len(texts) - 1, "combined answer")]

    monkeypatch.setattr(orchestrator, "handle_burst", fake_burst)
    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    monkeypatch.setattr(telegram_bot, "_BURST_WINDOW_S", 0.05)

    replies = []

    def make_update(text):
        async def reply_text(reply, reply_markup=None, do_quote=False):
            replies.append(reply)
        message = SimpleNamespace(text=text, reply_text=reply_text)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=9, type="private"),
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

    assert handled == [["but this is normal", "I can send message", "like this"]]
    assert replies == ["combined answer"]  # one reply for the whole burst


def _channel_harness(monkeypatch, chat_id=9):
    """Owner update factory + reply capture for _on_message tests."""
    from types import SimpleNamespace

    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    monkeypatch.setattr(telegram_bot, "_BURST_WINDOW_S", 0.05)
    replies = []

    def make_update(text):
        async def reply_text(reply, reply_markup=None, do_quote=False):
            replies.append(reply)
        message = SimpleNamespace(text=text, reply_text=reply_text)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=chat_id, type="private"),
            message=message,
        )

    class FakeBot:
        async def send_chat_action(self, chat_id, action):
            pass

    return make_update, SimpleNamespace(bot=FakeBot()), replies


async def test_late_fragment_supersedes_the_draft(monkeypatch):
    """The human rhythm: a new message landing while the reply is still
    being planned makes Kyraan stop, read it, and re-plan with the FULL
    thought — the first draft (covering only the early fragments) must
    never be sent."""
    from kyraan.agents import orchestrator

    make_update, ctx, replies = _channel_harness(monkeypatch)
    calls = []

    async def fake_burst(chat_id, texts, superseded=None):
        calls.append(list(texts))
        await asyncio.sleep(0.3)  # planning time — the late fragment lands here
        if superseded is not None and superseded.is_set():
            raise orchestrator.BurstSuperseded
        return [(len(texts) - 1, " + ".join(texts))]

    monkeypatch.setattr(orchestrator, "handle_burst", fake_burst)

    loop = asyncio.get_event_loop()
    first = loop.create_task(telegram_bot._on_message(make_update("today morning I have to go to siliguri"), ctx))
    await asyncio.sleep(0.2)  # window closes; composition begins
    late = loop.create_task(telegram_bot._on_message(make_update("to buy something"), ctx))
    await asyncio.gather(first, late)

    # Retracted once, then re-planned with the whole thought, one reply.
    assert calls[-1] == ["today morning I have to go to siliguri", "to buy something"]
    assert replies == ["today morning I have to go to siliguri + to buy something"]


async def test_fragment_after_the_safe_point_starts_the_next_round(monkeypatch):
    """A fragment too late to retract the reply must not sit unprocessed
    until a future message wakes the flusher — the same flusher drains it
    as a follow-up round."""
    from kyraan.agents import orchestrator

    make_update, ctx, replies = _channel_harness(monkeypatch)
    late_sent = False

    async def fake_burst(chat_id, texts, superseded=None):
        nonlocal late_sent
        if not late_sent:
            late_sent = True
            # Simulate the fragment arriving after the safe point: it
            # lands in the buffer but this composition finishes anyway.
            await telegram_bot._on_message(make_update("late follow-up"), ctx)
        return [(len(texts) - 1, " + ".join(texts))]

    monkeypatch.setattr(orchestrator, "handle_burst", fake_burst)
    await telegram_bot._on_message(make_update("first thought"), ctx)

    assert replies == ["first thought", "late follow-up"]


async def test_photo_message_gets_an_honest_reply_not_silence(monkeypatch):
    """Live: an image captioned 'can yiu tell me what is this' was simply
    ignored — the text-only handler never fires for media. Until vision
    lands, the honest limitation beats a dropped message."""
    from types import SimpleNamespace

    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    replies = []

    async def reply_text(reply, reply_markup=None, do_quote=False):
        replies.append(reply)

    message = SimpleNamespace(photo=[object()], voice=None, video=None,
                              audio=None, sticker=None, document=None,
                              reply_text=reply_text)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                             effective_chat=SimpleNamespace(id=9, type="private"), message=message)
    await telegram_bot._on_unsupported(update, SimpleNamespace(bot=None))
    assert len(replies) == 1 and "can't open a photo yet" in replies[0]

    # Non-owner media stays ignored.
    update.effective_user = SimpleNamespace(id=2)
    await telegram_bot._on_unsupported(update, SimpleNamespace(bot=None))
    assert len(replies) == 1


async def test_typing_starts_at_message_receipt(monkeypatch):
    """The indicator doubles as the 'seen' receipt — it must start when
    the message lands, not when composition begins (live: a ~2s frontier
    reply never showed typing at all)."""
    import asyncio as aio
    from types import SimpleNamespace
    from kyraan.agents import orchestrator

    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    monkeypatch.setattr(telegram_bot, "_BURST_WINDOW_S", 0.2)
    actions = []

    class FakeBot:
        async def send_chat_action(self, chat_id, action):
            actions.append(chat_id)

    async def fake_burst(chat_id, texts, superseded=None):
        return [(0, "reply")]

    monkeypatch.setattr(orchestrator, "handle_burst", fake_burst)

    async def reply_text(reply, reply_markup=None, do_quote=False):
        pass

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=9, type="private"),
        message=SimpleNamespace(text="hello", reply_text=reply_text))
    task = aio.get_event_loop().create_task(
        telegram_bot._on_message(update, SimpleNamespace(bot=FakeBot())))
    await aio.sleep(0.05)  # inside the gather window, before composition
    assert actions, "typing action must fire during the gather window"
    await task


async def test_owner_messages_in_a_group_are_ignored_entirely(monkeypatch):
    """External review P1: the owner-id check alone would have replied
    INTO a group — personal data and confirm flows in front of whoever
    else is there. Any non-private chat is ignored, owner or not."""
    from types import SimpleNamespace
    from kyraan.agents import orchestrator

    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)

    async def must_not_run(chat_id, texts, superseded=None):
        raise AssertionError("group message must never reach the orchestrator")

    monkeypatch.setattr(orchestrator, "handle_burst", must_not_run)
    replies = []

    async def reply_text(reply, reply_markup=None, do_quote=False):
        replies.append(reply)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),                       # the OWNER —
        effective_chat=SimpleNamespace(id=-100123, type="group"),   # but in a group
        message=SimpleNamespace(text="what's on my calendar?", reply_text=reply_text,
                                photo=None, voice=None, video=None, audio=None,
                                sticker=None, document=None))
    await telegram_bot._on_message(update, SimpleNamespace(bot=None))
    await telegram_bot._on_unsupported(update, SimpleNamespace(bot=None))
    assert replies == []


def test_startup_validation_catches_a_broken_tool_config(monkeypatch):
    """Review P2: invalid configuration fails loudly at boot, not latently
    at first use."""
    import pytest
    from kyraan.control_plane import config

    base = config.load()
    broken = {**base, "tools": {"t.x": {"description": "t", "server": "nope",
                                        "permission": "auto", "side_effects": "read",
                                        "params": {}, "failure": {"on_failure": "surface"}}}}
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "1")
    monkeypatch.setattr(config, "load", lambda: broken)
    from kyraan.tools import registry
    registry._adapter_module.cache_clear()
    try:
        with pytest.raises(ValueError, match="unknown server"):
            telegram_bot._validate_startup()
    finally:
        registry._adapter_module.cache_clear()


def test_startup_validation_rejects_unknown_kinds_and_missing_tiers(monkeypatch):
    """Round-4 P2: unknown provider kinds and an absent required tier must
    fail the boot."""
    import pytest
    from kyraan.control_plane import config

    monkeypatch.setenv("TELEGRAM_OWNER_ID", "1")
    base = config.load()

    bad_kind = {**base, "providers": {**base["providers"],
                                      "weird": {"kind": "quantum", "api_key_env": "X"}}}
    monkeypatch.setattr(config, "load", lambda: bad_kind)
    with pytest.raises(ValueError, match="unknown kind"):
        telegram_bot._validate_startup()

    no_cheap = {**base, "model_tiers": {"frontier": base["model_tiers"]["frontier"]}}
    monkeypatch.setattr(config, "load", lambda: no_cheap)
    with pytest.raises(ValueError, match="must define 'cheap'"):
        telegram_bot._validate_startup()


def test_startup_validation_rejects_keyless_remote_providers(monkeypatch):
    """Round-5 P2: a remote openai_compatible endpoint with no credential
    config must fail the boot, while localhost stays keyless-legal."""
    import pytest
    from kyraan.control_plane import config

    monkeypatch.setenv("TELEGRAM_OWNER_ID", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")  # active-tier check needs it
    base = config.load()
    keyless_remote = {**base, "providers": {**base["providers"],
        "sketchy": {"kind": "openai_compatible", "base_url": "https://api.example.com/v1"}}}
    monkeypatch.setattr(config, "load", lambda: keyless_remote)
    with pytest.raises(ValueError, match="no api_key_env"):
        telegram_bot._validate_startup()

    # Round 6: keyless must be DECLARED — a localhost proxy without the
    # flag fails too, and the declaration makes it legal anywhere.
    keyless_local = {**base, "providers": {**base["providers"],
        "proxy": {"kind": "openai_compatible", "base_url": "http://localhost:9999/v1"}}}
    monkeypatch.setattr(config, "load", lambda: keyless_local)
    with pytest.raises(ValueError, match="allow_unauthenticated"):
        telegram_bot._validate_startup()

    declared = {**base, "providers": {**base["providers"],
        "proxy": {"kind": "openai_compatible", "base_url": "http://localhost:9999/v1",
                  "allow_unauthenticated": True}}}
    monkeypatch.setattr(config, "load", lambda: declared)
    telegram_bot._validate_startup()   # passes with the explicit declaration

    # Round 7: truthy is not true — a mistyped flag must not bypass.
    for bad_value in ("false", "true", 1):
        mistyped = {**base, "providers": {**base["providers"],
            "proxy": {"kind": "openai_compatible", "base_url": "http://localhost:9999/v1",
                      "allow_unauthenticated": bad_value}}}
        monkeypatch.setattr(config, "load", lambda m=mistyped: m)
        with pytest.raises(ValueError, match="allow_unauthenticated"):
            telegram_bot._validate_startup()
