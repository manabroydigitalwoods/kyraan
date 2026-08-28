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
    orchestrator._confirmation_nonce[77] = "abc123def456"
    try:
        kb = telegram_bot._confirm_keyboard(77)
        buttons = kb.inline_keyboard[0]
        # buttons carry the pending action's nonce (security round P1:
        # a static callback let an old Yes confirm a newer action)
        assert [b.callback_data for b in buttons] == [
            "kyraan_yes:abc123def456", "kyraan_no:abc123def456"]
    finally:
        orchestrator._pending_confirmations.pop(77, None)
        orchestrator._confirmation_nonce.pop(77, None)


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
    first = loop.create_task(telegram_bot._on_message(make_update("today morning I have to go to nagpur"), ctx))
    await asyncio.sleep(0.2)  # window closes; composition begins
    late = loop.create_task(telegram_bot._on_message(make_update("to buy something"), ctx))
    await asyncio.gather(first, late)

    # Retracted once, then re-planned with the whole thought, one reply.
    assert calls[-1] == ["today morning I have to go to nagpur", "to buy something"]
    assert replies == ["today morning I have to go to nagpur + to buy something"]


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


async def test_stale_confirmation_button_is_rejected(monkeypatch):
    """Security round P1: a Yes button from an earlier ask must never
    confirm the current pending action."""
    from types import SimpleNamespace
    from kyraan.agents import orchestrator

    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    orchestrator._confirmation_nonce[9] = "current-nonce"

    async def must_not_run(chat_id, word):
        raise AssertionError("a stale button must never reach the confirm flow")

    monkeypatch.setattr(orchestrator, "handle_message", must_not_run)
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append(text)

    async def answer():
        pass

    async def edit(reply_markup=None):
        pass

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=9, type="private"),
        callback_query=SimpleNamespace(data="kyraan_yes:OLD-nonce", answer=answer,
                                       edit_message_reply_markup=edit))
    await telegram_bot._on_callback(update, SimpleNamespace(bot=FakeBot()))
    assert sent and "no longer active" in sent[0]
    orchestrator._confirmation_nonce.pop(9, None)


async def test_nonce_race_old_button_cannot_confirm_swapped_action(monkeypatch):
    """Security round 3, P1 — the RACE, not just the stale check: while an
    old Yes waits on the per-chat lock, the pending action is replaced;
    on acquiring the lock the old button must be rejected. (The previous
    'fix' for this passed review twice while never being applied — this
    test exercises the interleaving itself.)"""
    import asyncio
    from types import SimpleNamespace
    from kyraan.agents import orchestrator

    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    chat = 9
    orchestrator._confirmation_nonce[chat] = "OLD"
    confirmed = []

    async def fake_handle(chat_id, word):
        confirmed.append(word)
        return "executed"

    monkeypatch.setattr(orchestrator, "handle_message", fake_handle)
    lock = telegram_bot._lock_for(chat)
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append(text)

    async def answer():
        pass

    async def edit(reply_markup=None):
        pass

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=chat, type="private"),
        callback_query=SimpleNamespace(data="kyraan_yes:OLD", answer=answer,
                                       edit_message_reply_markup=edit))

    async def swap_pending_while_locked():
        async with lock:
            # the old button's callback starts NOW, and must wait on us
            task = asyncio.create_task(
                telegram_bot._on_callback(update, SimpleNamespace(bot=FakeBot())))
            await asyncio.sleep(0.05)
            # a NEW pending action replaces the old one before we release
            orchestrator._confirmation_nonce[chat] = "NEW"
            return task

    task = await swap_pending_while_locked()
    await task
    assert confirmed == []                      # the swapped action was NOT confirmed
    assert sent and "no longer active" in sent[0]
    orchestrator._confirmation_nonce.pop(chat, None)


async def test_voice_note_becomes_text_in_the_normal_pipeline(monkeypatch):
    """A voice note is transcribed LOCALLY and then flows through the
    exact same pipeline as a typed message — same brain, same guards."""
    from types import SimpleNamespace
    from kyraan.agents import orchestrator
    from kyraan.channels import voice

    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    monkeypatch.setattr(telegram_bot, "_BURST_WINDOW_S", 0.05)

    async def probe_ok(timeout: float = 90.0):
        return True

    # the handler's seam is wait_available — patching available() left
    # the real probe running, which only passed on machines WITH mlx
    # installed (the CI red)
    monkeypatch.setattr(voice, "wait_available", probe_ok)

    async def fake_transcribe(path):
        return "remind me to call Rohan tomorrow at nine am"

    monkeypatch.setattr(voice, "transcribe", fake_transcribe)
    handled = []

    async def fake_burst(chat_id, texts, superseded=None):
        handled.append(texts)
        return [(0, "Reminder noted.")]

    monkeypatch.setattr(orchestrator, "handle_burst", fake_burst)
    replies = []

    async def reply_text(reply, reply_markup=None, do_quote=False):
        replies.append(reply)

    class FakeTgFile:
        async def download_to_drive(self, custom_path=None):
            custom_path.write_bytes(b"fake-oga")

    class FakeVoice:
        async def get_file(self):
            return FakeTgFile()

    class FakeBot:
        async def send_chat_action(self, chat_id, action):
            pass

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=9, type="private"),
        message=SimpleNamespace(voice=FakeVoice(), reply_text=reply_text))
    await telegram_bot._on_voice(update, SimpleNamespace(bot=FakeBot()))
    assert handled == [["remind me to call Rohan tomorrow at nine am"]]
    assert replies == ["Reminder noted."]


async def test_voice_unavailable_and_empty_transcripts_stay_honest(monkeypatch):
    from types import SimpleNamespace
    from kyraan.channels import voice

    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    replies = []

    async def reply_text(reply, reply_markup=None, do_quote=False):
        replies.append(reply)

    class FakeTgFile:
        async def download_to_drive(self, custom_path=None):
            custom_path.write_bytes(b"x")

    class FakeVoice:
        async def get_file(self):
            return FakeTgFile()

    class FakeBot:
        async def send_chat_action(self, chat_id, action):
            pass

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=9, type="private"),
        message=SimpleNamespace(voice=FakeVoice(), reply_text=reply_text))

    async def not_available(timeout: float = 90.0):
        return False

    # the handler waits on the probe (a note in hand is worth waiting
    # for) rather than reading the instantaneous available()
    monkeypatch.setattr(voice, "wait_available", not_available)
    await telegram_bot._on_voice(update, SimpleNamespace(bot=FakeBot()))
    assert "can't listen to voice notes yet" in replies[0]

    async def now_available(timeout: float = 90.0):
        return True

    monkeypatch.setattr(voice, "wait_available", now_available)

    async def empty(path):
        return ""

    monkeypatch.setattr(voice, "transcribe", empty)
    await telegram_bot._on_voice(update, SimpleNamespace(bot=FakeBot()))
    assert "couldn't make out" in replies[1]


async def test_deliver_retries_once_then_records_the_divergence(monkeypatch):
    """CommitKernel-lite (2026-08-28): execution status and delivery
    status are different facts — a dropped receipt after an executed
    write gets one retry, then a greppable reply_delivery_failed event
    carrying the undelivered text."""
    import json
    from kyraan.control_plane import logging_setup

    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Timed out")

    ok = await telegram_bot._deliver(7, flaky, "Done — the ac is off.")
    assert ok is True and len(attempts) == 2

    async def dead():
        raise RuntimeError("Bad Gateway")

    ok = await telegram_bot._deliver(7, dead, "Done — the ac is off.")
    assert ok is False
    events = [json.loads(l) for l in
              logging_setup.EVENT_LOG.read_text().splitlines()]
    failed = [e for e in events if e["kind"] == "reply_delivery_failed"]
    assert failed and failed[-1]["undelivered"] == "Done — the ac is off."


async def test_enrolled_person_reminders_are_delivered(monkeypatch):
    """Bugbot P1 (2026-08-28): reminders.create is in every viewer
    stage's toolset, but the owner-era send gate silently retired any
    non-owner chat's reminder — a viewer could set one and never
    receive it. Admitted enrolled chats now deliver; unknown chats
    (dev-harness records) still retire."""
    from types import SimpleNamespace
    from kyraan.store import persons
    from kyraan.triggers import scheduler

    monkeypatch.setattr(telegram_bot, "_owner_id", lambda: 1)
    monkeypatch.setattr(persons, "person_for_chat",
                        lambda cid, strict=False:
                        ("ruma", "full") if cid == 891 else None)
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    captured = {}
    monkeypatch.setattr(scheduler, "init",
                        lambda schedule_fn, cancel_fn, send_fn:
                        captured.update(send=send_fn))
    monkeypatch.setattr(telegram_bot.orchestrator, "record_proactive",
                        lambda cid, text: None)

    class FakeJQ:
        def run_once(self, *a, **k): pass
        def get_jobs_by_name(self, n): return []

    telegram_bot._wire_scheduler(FakeJQ(), FakeBot())
    send_fn = captured["send"]
    assert await send_fn(1, "owner reminder") is True
    assert await send_fn(891, "ruma reminder") is True     # enrolled: delivered
    assert await send_fn(9999, "harness record") is False  # unknown: retired
    assert [c for c, _ in sent] == [1, 891]

    # round-2 P1: a store OUTAGE must RAISE (transient -> fire retries),
    # never return False (permanent retire)
    def broken_lookup(cid, strict=False):
        raise RuntimeError("pg down")

    monkeypatch.setattr(persons, "person_for_chat", broken_lookup)
    with pytest.raises(RuntimeError):
        await send_fn(891, "ruma reminder during outage")
