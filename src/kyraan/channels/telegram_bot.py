"""Single channel for Phase 1. Restricted to TELEGRAM_OWNER_ID so this stays
a personal assistant, not an open bot, until Phase 3's multi-user work.
"""
import asyncio
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    JobQueue,
    MessageHandler,
    filters,
)

from kyraan.agents import orchestrator
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.logging_setup import get_logger
from kyraan.triggers import briefs, scheduler

logger = get_logger("telegram_bot")


def _owner_id() -> int:
    return int(os.environ["TELEGRAM_OWNER_ID"])


def _owner_private(update: Update) -> bool:
    """Owner id AND a private chat. The id check alone let the owner's
    messages in any group trigger replies INTO that group — personal
    data and confirm flows in front of whoever else is there (external
    review P1). Group/channel updates are ignored outright."""
    return (update.effective_user is not None
            and update.effective_user.id == _owner_id()
            and update.effective_chat is not None
            and getattr(update.effective_chat, "type", None) == "private")


def _plain(text: str) -> str:
    """Models emit markdown bold/italic; replies are sent without
    parse_mode (entity-parse errors on unbalanced markers would eat whole
    messages), so Telegram shows the raw asterisks — strip them."""
    return text.replace("**", "").replace("__", "")


async def _typing_loop(bot, chat_id: int) -> None:
    """Keep the "Kyraan is typing…" indicator alive while a reply is being
    produced — Telegram expires a chat action after ~5s, and model + tool
    time regularly exceeds that. Cancelled the moment the reply is ready;
    the indicator also tells the owner their message was received (bots
    can't mark messages "read" — that's a Business-API-only feature)."""
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


def _confirm_keyboard(chat_id: int) -> InlineKeyboardMarkup | None:
    """Yes/No buttons whenever a confirmation is pending for this chat —
    tap instead of typing, and a tap is unambiguous in a way a later
    typed "yes" never is."""
    if chat_id not in orchestrator._pending_confirmations:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data="kyraan_yes"),
        InlineKeyboardButton("❌ No", callback_data="kyraan_no"),
    ]])


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _owner_private(update):
        await query.answer()
        return
    await query.answer()
    word = "yes" if query.data == "kyraan_yes" else "no"
    # Remove the buttons from the ask so a decided confirmation can't be
    # tapped twice, then run the exact same path a typed yes/no takes.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # message may be old/edited — the confirm flow still decides
    chat_id = update.effective_chat.id
    async with _lock_for(chat_id):
        reply = await orchestrator.handle_message(chat_id, word)
    await context.bot.send_message(chat_id=chat_id, text=_plain(reply))


# Burst coalescing, modeled on how two humans chat: B watches A's typing
# indicator and replies only when A's thought looks COMPLETE; if a new
# message lands while B is mid-reply, B stops, reads it, and rethinks.
# Telegram never sends bots the typing indicator, so both halves are
# inferred: thought-completeness from the message's shape
# (orchestrator.thought_open), and "stop and rethink" from a fragment
# arriving while a reply is still being planned (supersede — the draft is
# retracted and re-planned with the full thought). Requires
# concurrent_updates so later fragments can join while a window is open;
# a per-chat lock keeps actual processing strictly serialized.
_BURST_WINDOW_S = 2.5           # typing a follow-up message takes 2-5s
_BURST_MAX_WAIT_S = 8.0
_FRAGMENT_EXTRA_WAIT_S = 10.0   # an open thought almost certainly has
                                # more coming — wait patiently for it
_burst_buffers: dict = {}
_burst_flushing: set = set()
_burst_superseded: dict = {}
_chat_locks: dict = {}


def _lock_for(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_private(update):
        logger.warning("Ignored non-owner or non-private update (user=%s chat=%s)",
                       update.effective_user, update.effective_chat)
        return

    chat_id = update.effective_chat.id
    _burst_buffers.setdefault(chat_id, []).append((update.message, update.message.text or ""))
    if chat_id in _burst_flushing:
        # A window is open or a reply is being composed. The new fragment
        # supersedes any draft: composition checks the event at its safe
        # point (before anything with side effects runs) and starts over
        # with the full thought.
        event = _burst_superseded.get(chat_id)
        if event is not None:
            event.set()
        return
    _burst_flushing.add(chat_id)
    # Typing starts the moment the message lands — it doubles as the
    # "seen" receipt (bots can't mark messages read). Starting it only at
    # composition made it invisible: the fast frontier answers in ~2s and
    # the indicator never got long enough on screen to render.
    typing = asyncio.create_task(_typing_loop(context.bot, chat_id))
    try:
        while True:
            # Gather: wait for quiet, patiently while the last message
            # reads as an unfinished thought — the substitute for watching
            # the typing indicator, which Telegram never sends bots.
            waited = 0.0
            last_text = ""
            while waited < _BURST_MAX_WAIT_S * 3:  # hard cap ~20s+
                buffered = _burst_buffers.get(chat_id, [])
                seen = len(buffered)
                last_text = buffered[-1][1] if buffered else ""
                quiet = _BURST_WINDOW_S
                if seen > 1 or orchestrator.thought_open(last_text):
                    quiet = _BURST_WINDOW_S * 2
                await asyncio.sleep(quiet)
                waited += quiet
                if len(_burst_buffers.get(chat_id, [])) == seen:
                    if orchestrator.thought_open(last_text) and waited < _FRAGMENT_EXTRA_WAIT_S:
                        continue  # thought still open — keep waiting
                    break  # quiet and complete — reply now
            # The event exists BEFORE the buffer is popped, so a fragment
            # arriving in between lands in this pop, and one arriving
            # after it sets the event — no gap either way.
            event = _burst_superseded[chat_id] = asyncio.Event()
            fragments = _burst_buffers.pop(chat_id, [])
            if not fragments:
                return
            try:
                async with _lock_for(chat_id):
                    # The burst is evaluated TOGETHER and answered as ONE
                    # composed reply, quoted onto the message it covers.
                    results = await orchestrator.handle_burst(
                        chat_id, [text for _, text in fragments], superseded=event
                    )
            except orchestrator.BurstSuperseded:
                # The user kept typing while the reply was being planned —
                # nothing ran yet, so retract the draft, fold these
                # fragments back in front of the newcomers, and re-read
                # the whole thought (the typing indicator stays on: Kyraan
                # is still working on a reply).
                _burst_buffers[chat_id] = fragments + _burst_buffers.get(chat_id, [])
                continue
            for position, (idx, reply) in enumerate(results):
                source = fragments[min(idx, len(fragments) - 1)][0]
                markup = _confirm_keyboard(chat_id) if position == len(results) - 1 else None
                await source.reply_text(_plain(reply), reply_markup=markup, do_quote=True)
            # A fragment that arrived after composition passed its safe
            # point couldn't retract this reply — it starts the next round
            # now (with this reply already in context) instead of sitting
            # unprocessed until some future message wakes the flusher.
            if not _burst_buffers.get(chat_id):
                return
    finally:
        _burst_flushing.discard(chat_id)
        _burst_superseded.pop(chat_id, None)
        if typing is not None:
            typing.cancel()


async def _on_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photos, voice notes, stickers, files — the text-only handler never
    fires for these, and the owner got SILENCE (live 2026-08-26: an image
    with 'can yiu tell me what is this' was simply ignored). Until vision
    and voice land (Phase 5), the honest reply beats a dropped message."""
    if not _owner_private(update):
        return
    message = update.message
    if message is None:
        return
    kind = ("a photo" if message.photo else
            "a voice message" if message.voice else
            "a video" if message.video else
            "an audio file" if message.audio else
            "a sticker" if message.sticker else
            "a file" if message.document else "that kind of message")
    logger.info("Unsupported media from owner: %s", kind)
    await message.reply_text(
        f"I can't open {kind} yet — I only read text for now (seeing images "
        "and hearing voice notes come in a later phase). Tell me in words "
        "and I'm all yours.",
        do_quote=True,
    )


async def _reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    await scheduler.fire(data["reminder_id"], data["chat_id"], data["text"])


def _wire_scheduler(job_queue: JobQueue, bot) -> None:
    def schedule_fn(job_name: str, run_at, payload: dict) -> None:
        job_queue.run_once(_reminder_job, when=run_at, data=payload, name=job_name)

    def cancel_fn(job_name: str) -> None:
        for job in job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()

    async def send_fn(chat_id: int, text: str) -> bool:
        # The store is shared with the dev harnesses (chat.py uses chat 0,
        # walkthrough scripts use their own ids). A record like that would
        # make send_message error on a nonexistent chat and leave the
        # reminder pending forever, retried on every restart. The bot
        # delivers to its owner only; anything else is retired — and the
        # False return makes fire() log it truthfully as
        # reminder_retired_undelivered, not reminder_sent.
        if chat_id != _owner_id():
            logger.warning("Retiring reminder for non-owner chat %s (dev-harness record)", chat_id)
            return False
        await bot.send_message(chat_id=chat_id, text=text)
        orchestrator.record_proactive(chat_id, text)
        return True

    scheduler.init(schedule_fn=schedule_fn, cancel_fn=cancel_fn, send_fn=send_fn)


def _wire_brief(job_queue: JobQueue, bot) -> None:
    at = briefs.brief_time()
    if at is None:
        return

    async def _brief_job(context: ContextTypes.DEFAULT_TYPE) -> None:
        async def send_fn(chat_id: int, text: str) -> None:
            await context.bot.send_message(chat_id=chat_id, text=text)
            orchestrator.record_proactive(chat_id, text)

        await briefs.fire(_owner_id(), send_fn)

    # run_daily needs a tz-aware time or it fires in UTC — the whole point
    # is 07:30 on the owner's clock.
    job_queue.run_daily(_brief_job, time=at.replace(tzinfo=local_now().tzinfo), name="morning_brief")
    logger.info("Morning brief scheduled daily at %s %s", at, local_now().tzinfo)


def _validate_startup() -> None:
    """Fail LOUDLY at boot instead of latently at first use (review P2):
    the tool registry's load-time validation (unknown servers, auto
    writes, bad fallbacks) and the model-tier wiring both run now."""
    from kyraan.control_plane import config
    from kyraan.tools import registry

    int(os.environ["TELEGRAM_OWNER_ID"])  # KeyError/ValueError = boot failure

    tools = registry.load()
    cfg = config.load()
    tiers = cfg["model_tiers"]
    providers = cfg["providers"]
    for required in ("cheap", "frontier"):
        if required not in tiers:
            raise ValueError(f"model_tiers must define {required!r} — the fallback chain depends on it")
    _KNOWN_KINDS = {"anthropic", "gemini", "openai_compatible", "ollama_native"}
    for pname, provider in providers.items():
        kind = provider.get("kind")
        if kind not in _KNOWN_KINDS:
            raise ValueError(f"provider {pname!r} has unknown kind {kind!r}")
        if kind in ("anthropic", "gemini") and not provider.get("api_key_env"):
            raise ValueError(f"provider {pname!r} ({kind}) declares no api_key_env")
        if kind == "openai_compatible" and not provider.get("api_key_env"):
            base = provider.get("base_url") or ""
            host = base.split("//")[-1].split("/")[0].split(":")[0]
            if not (host == "localhost" or host == "127.0.0.1" or host.endswith(".localhost")):
                # A remote endpoint with no credential config would pass
                # boot and die at first use (round-5 P2).
                raise ValueError(
                    f"provider {pname!r} points at remote {base!r} with no api_key_env")
    for name, tier in tiers.items():
        provider = providers.get(tier.get("provider"))
        if provider is None:
            raise ValueError(f"model tier {name!r} names unknown provider {tier.get('provider')!r}")
        if not tier.get("model"):
            raise ValueError(f"model tier {name!r} has no model")
        key_env = provider.get("api_key_env")
        if key_env and not os.environ.get(key_env, "").strip():
            raise ValueError(
                f"model tier {name!r} needs {key_env} in .env (provider {tier['provider']!r})")

    import importlib
    for server_name, server in (cfg.get("tool_servers") or {}).items():
        if server.get("transport") == "builtin" and server.get("module"):
            importlib.import_module(server["module"])  # ImportError = boot failure

    logger.info("Startup validation: %d tools, %d model tiers, owner id OK",
                len(tools), len(tiers))


def run() -> None:
    _validate_startup()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VOICE | filters.VIDEO | filters.AUDIO
        | filters.Sticker.ALL | filters.Document.ALL,
        _on_unsupported,
    ))
    app.add_handler(CallbackQueryHandler(_on_callback, pattern="^kyraan_(yes|no)$"))

    _wire_scheduler(app.job_queue, app.bot)
    _wire_brief(app.job_queue, app.bot)
    # A restart must be invisible to the owner: reload the conversation
    # from chat.jsonl so follow-ups ("are those the latest emails?") still
    # have their context.
    orchestrator.seed_history_from_log()
    from kyraan.memory import engine
    engine.migrate_from_tree()  # one-time index backfill; no-op after

    logger.info("Kyraan Telegram bot starting (owner-only, Phase 1)")
    app.run_polling()
