"""Single channel for Phase 1. Restricted to TELEGRAM_OWNER_ID so this stays
a personal assistant, not an open bot, until Phase 3's multi-user work.
"""
import os

from telegram import Update
from telegram.ext import Application, ContextTypes, JobQueue, MessageHandler, filters

from kyraan.agents import orchestrator
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.logging_setup import get_logger
from kyraan.triggers import briefs, scheduler

logger = get_logger("telegram_bot")


def _owner_id() -> int:
    return int(os.environ["TELEGRAM_OWNER_ID"])


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_user.id != _owner_id():
        logger.warning("Ignored message from non-owner user %s", update.effective_user)
        return

    chat_id = update.effective_chat.id
    text = update.message.text or ""
    reply = await orchestrator.handle_message(chat_id, text)
    await update.message.reply_text(reply)


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
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))

    _wire_scheduler(app.job_queue, app.bot)
    _wire_brief(app.job_queue, app.bot)

    logger.info("Kyraan Telegram bot starting (owner-only, Phase 1)")
    app.run_polling()
