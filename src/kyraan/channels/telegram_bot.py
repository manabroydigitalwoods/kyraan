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
    if update.effective_user is None or update.effective_user.id != _owner_id():
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
    await context.bot.send_message(chat_id=chat_id, text=reply)


# Burst coalescing: humans send thoughts as several quick messages
# ("but this is normal" / "I can send message" / "like this") — answering
# each fragment separately serially is what made a live session feel like
# "randomly answering". Messages arriving within the debounce window are
# combined and answered as ONE message, exactly how a person reads a
# burst. Requires concurrent_updates so later fragments can join the
# buffer while the window is open; a per-chat lock keeps actual
# processing strictly serialized.
_BURST_WINDOW_S = 2.5           # typing a follow-up message takes 2-5s
_BURST_MAX_WAIT_S = 8.0
_FRAGMENT_EXTRA_WAIT_S = 10.0   # a bare time-phrase almost certainly has
                                # more coming — wait patiently for it
_burst_buffers: dict = {}
_burst_flushing: set = set()
_chat_locks: dict = {}


def _lock_for(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_user.id != _owner_id():
        logger.warning("Ignored message from non-owner user %s", update.effective_user)
        return

    chat_id = update.effective_chat.id
    _burst_buffers.setdefault(chat_id, []).append((update.message, update.message.text or ""))
    if chat_id in _burst_flushing:
        return  # an open window will pick this fragment up
    _burst_flushing.add(chat_id)
    try:
        waited = 0.0
        while waited < _BURST_MAX_WAIT_S * 3:  # hard cap ~20s+
            buffered = _burst_buffers[chat_id]
            seen = len(buffered)
            # An active burst (>1 message) or an open fragment gets a
            # longer quiet requirement — the user is mid-thought.
            combined_now = "\n".join(t for _, t in buffered if t)
            quiet = _BURST_WINDOW_S
            if seen > 1 or orchestrator.is_time_fragment(combined_now):
                quiet = _BURST_WINDOW_S * 2
            await asyncio.sleep(quiet)
            waited += quiet
            if len(_burst_buffers[chat_id]) == seen:
                if orchestrator.is_time_fragment(combined_now) and waited < _FRAGMENT_EXTRA_WAIT_S:
                    continue  # fragment stays open a while longer
                break  # quiet — the thought is complete
        fragments = _burst_buffers.pop(chat_id, [])
    finally:
        _burst_flushing.discard(chat_id)
    if not fragments:
        return

    typing = asyncio.create_task(_typing_loop(context.bot, chat_id))
    try:
        async with _lock_for(chat_id):
            # The burst is evaluated TOGETHER; the resolver decides one
            # combined answer vs per-message answers, and each reply is
            # quoted onto the message it covers.
            results = await orchestrator.handle_burst(
                chat_id, [text for _, text in fragments]
            )
    finally:
        typing.cancel()
    for position, (idx, reply) in enumerate(results):
        source = fragments[min(idx, len(fragments) - 1)][0]
        markup = _confirm_keyboard(chat_id) if position == len(results) - 1 else None
        await source.reply_text(reply, reply_markup=markup, do_quote=True)


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


def run() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    app.add_handler(CallbackQueryHandler(_on_callback, pattern="^kyraan_(yes|no)$"))

    _wire_scheduler(app.job_queue, app.bot)
    _wire_brief(app.job_queue, app.bot)

    logger.info("Kyraan Telegram bot starting (owner-only, Phase 1)")
    app.run_polling()
