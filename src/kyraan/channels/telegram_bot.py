"""Single channel for Phase 1. Restricted to TELEGRAM_OWNER_ID so this stays
a personal assistant, not an open bot, until Phase 3's multi-user work.
"""
import asyncio
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    JobQueue,
    MessageHandler,
    filters,
)

from kyraan.agents import orchestrator
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.logging_setup import get_logger, log_event
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


def _authorized(update: Update) -> str | None:
    """P3.5a: who this update is from — 'owner' (env check, unchanged,
    never touches a store), an enrolled person id at stage >=
    read_mostly in a PRIVATE chat, or None (rejected exactly as before
    Phase 3). Media/voice/location handlers stay owner-only until
    P3.5b/c scope tools and visibility for other stages."""
    if _owner_private(update):
        return "owner"
    if (update.effective_chat is None
            or getattr(update.effective_chat, "type", None) != "private"
            or update.effective_user is None
            or update.effective_user.id != update.effective_chat.id):
        return None
    from kyraan.store import persons
    row = persons.person_for_chat(update.effective_chat.id)
    return row[0] if row else None


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
    nonce = orchestrator._confirmation_nonce.get(chat_id, "")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"kyraan_yes:{nonce}"),
        InlineKeyboardButton("❌ No", callback_data=f"kyraan_no:{nonce}"),
    ]])


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if _authorized(update) is None:
        await query.answer()
        return
    await query.answer()
    action, _, nonce = (query.data or "").partition(":")
    chat_id = update.effective_chat.id
    word = "yes" if action == "kyraan_yes" else "no"
    # Remove the buttons from the ask so a decided confirmation can't be
    # tapped twice.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # message may be old/edited — the confirm flow still decides
    # Nonce validated INSIDE the per-chat lock (security rounds 2-3, P1):
    # checked outside, a concurrent message could replace the pending
    # action while this old button waited on the lock — and the stale Yes
    # would then confirm the NEWER action.
    async with _lock_for(chat_id):
        if nonce != orchestrator.current_confirmation_nonce(chat_id):
            await context.bot.send_message(
                chat_id=chat_id,
                text="That button belongs to an earlier ask and is no longer "
                     "active — reply to the latest confirmation instead.")
            return
        from kyraan.control_plane import kernel as _kernel
        stage_token = _kernel.set_viewer_stage(_viewer_stage_for(update))
        try:
            reply = await orchestrator.handle_message(chat_id, word)
        finally:
            _kernel.reset_viewer_stage(stage_token)
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


_courtesy_sent: dict = {}  # chat_id -> local date of the one daily line


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    person = _authorized(update)
    if person is None:
        logger.warning("Ignored unauthorized update (user=%s chat=%s)",
                       update.effective_user, update.effective_chat)
        # KNOWN-but-unenrolled (stage 'none' with this chat_id recorded):
        # a family member gets one courtesy line a day instead of pure
        # silence (live 2026-08-27: Ruma messaged from her own phone and
        # concluded the bot was broken). Strangers still get nothing.
        try:
            chat_id = update.effective_chat.id
            from kyraan.store import pg as _pg
            with _pg.connection() as conn:
                row = conn.execute(
                    "SELECT id FROM person WHERE chat_id = %s AND stage = 'none'",
                    (chat_id,)).fetchone()
            today = local_now().date().isoformat()
            if row and _courtesy_sent.get(chat_id) != today:
                _courtesy_sent[chat_id] = today
                await update.message.reply_text(
                    "Hi — I'm Manab's personal assistant and can only talk "
                    "to people he has enrolled. Ask him to switch you on!")
        except Exception:
            pass  # a courtesy must never error
        return
    if person != "owner":
        # An enrolled person gets the TEXT pipeline; face enrollment is
        # a biometric write and stays an owner ceremony (P3.5a scope —
        # tool/visibility scoping per stage lands in P3.5b/c).
        await _ingest(update, context, update.message.text or "")
        return

    from kyraan.agents import faces, orchestrator
    name = faces.enroll_from_text(update.message.text or "")
    if name:
        chat_id = update.effective_chat.id
        stashed = faces.recent_photo(chat_id)
        if stashed is not None:
            reply = await _enroll_face_gated(chat_id, name, stashed)
            orchestrator.record_exchange(chat_id, update.message.text or "", reply)
            await update.message.reply_text(
                _plain(reply), do_quote=True,
                reply_markup=_confirm_keyboard(chat_id))
            return
        # No recent photo: fall through — it may be an ordinary memory
        # statement, and the normal pipeline handles those.

    await _ingest(update, context, update.message.text or "")


def _viewer_stage_for(update: Update) -> str:
    """P3.5b: the stage the kernel scopes this turn to. Owner (env check)
    is unscoped; an enrolled chat gets its enrolled stage; anything else
    is 'none' (belt — such updates were already rejected upstream)."""
    if _owner_private(update):
        return "owner"
    from kyraan.store import persons
    row = persons.person_for_chat(update.effective_chat.id)
    return row[1] if row else "none"


async def _ingest(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Everything after the ownership gate — shared by typed messages and
    transcribed voice notes (which ARE text by the time they get here)."""
    from kyraan.control_plane import kernel
    stage_token = kernel.set_viewer_stage(_viewer_stage_for(update))
    try:
        await _ingest_inner(update, context, text)
    finally:
        kernel.reset_viewer_stage(stage_token)


async def _ingest_inner(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = update.effective_chat.id
    _burst_buffers.setdefault(chat_id, []).append((update.message, text))
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


_HELP_TEXT = """Hi — I'm Kyraan, your personal assistant. Talk to me like a person (voice notes work too). Things I do:

⏰ Reminders — "remind me to call mom at 7pm", "every day at 9 take medicine", "any reminders?", "cancel the call-mom one"
📅 Calendar — "what's on tomorrow?", "add lunch with Mira friday 1pm", "cancel the 3pm meeting" (I always ask before changing anything)
📬 Email — "any new emails?" (senders & subjects only — I never read bodies)
🏠 Home — "is the AC on?", "check energy", "bedroom temp", "turn off the AC"
🧠 Memory — tell me facts and I'll remember after you approve ("review memory"); "forget X" removes one
📊 Usage — "how much did we spend on AI this week?"
🌅 Briefs — mornings 7:30 and evenings 9:30, plus AC/energy heads-ups

Everything runs on your own computer; nothing is shared."""


async def _on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start and /help — a bot whose very first expected message gets
    silence feels broken (completion pack)."""
    if update.effective_user is None or update.effective_chat is None:
        return
    if not _owner_private(update):
        await update.message.reply_text("I'm a private personal assistant — not open for general use.")
        return
    await update.message.reply_text(_HELP_TEXT)


async def _on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A voice note becomes text locally (audio never leaves the Mac) and
    then flows through the exact same pipeline as a typed message."""
    if not _owner_private(update):
        return
    from kyraan.channels import voice

    if not await voice.wait_available():
        await update.message.reply_text(
            "I can't listen to voice notes yet on this machine — tell me in "
            "words for now.", do_quote=True)
        return
    import tempfile
    from pathlib import Path

    typing = asyncio.create_task(_typing_loop(context.bot, update.effective_chat.id))
    try:
        tg_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            from kyraan.control_plane.logging_setup import stage as _vstage
            with _vstage("voice_transcribe"):
                await tg_file.download_to_drive(custom_path=temp_path)
                text = await voice.transcribe(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)  # transient: no audio at rest
    finally:
        typing.cancel()
    if not text:
        await update.message.reply_text(
            "I couldn't make out that voice note — mind trying again, or "
            "typing it?", do_quote=True)
        return
    logger.info("Voice note transcribed (%d chars)", len(text))
    await _ingest(update, context, text)


async def _on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A shared location pin becomes text — place name via reverse
    geocoding (best-effort, coordinates as the fallback) — and flows
    through the same pipeline as a typed message, so a pin plus a caption
    like "weather here?" bursts into one thought. Live-location EDITS are
    not tracked; the initial pin is what the assistant gets. (Seen live
    2026-08-26: a shared pin matched no handler, was silently dropped,
    and the model kept asking which area the owner was in.)"""
    if not _owner_private(update):
        return
    if update.message is None:  # a live-location EDIT arrives as
        return                  # edited_message — initial pin only
    from kyraan.channels import location as geo

    venue = getattr(update.message, "venue", None)
    pin = update.message.location or (venue.location if venue else None)
    if pin is None:
        return
    if venue and venue.title:
        # Telegram's "choose a nearby place" sends a VENUE, not a bare
        # location — it went to NO handler and vanished silently (seen
        # live 2026-08-27: a shared place produced nothing at all). The
        # venue even names itself; use that over reverse geocoding.
        described = (f"{venue.title}"
                     + (f", {venue.address}" if venue.address else "")
                     + f" ({pin.latitude:.5f}, {pin.longitude:.5f})")
    else:
        typing = asyncio.create_task(_typing_loop(context.bot, update.effective_chat.id))
        try:
            from kyraan.control_plane.logging_setup import stage as _gstage
            with _gstage("geocode_pin"):
                described = await asyncio.to_thread(geo.describe, pin.latitude, pin.longitude)
        finally:
            typing.cancel()
    logger.info("Location pin resolved: %s", described)
    await _ingest(update, context,
                  f"[I'm sharing my current location: {described}]")


async def _enroll_face_gated(chat_id: int, name: str, image_bytes: bytes) -> str:
    """Face enrollment through the standard confirm flow — the owner's
    "yes" (typed, or the inline button) runs the stashed enrollment with
    these exact bytes. The template is written only after that yes."""
    from kyraan.agents import faces, orchestrator
    from kyraan.control_plane.kernel import SkillCall

    if not faces.available():
        return ("Face recognition isn't set up on this machine yet — run "
                "scripts/setup_faces.py once, then send the photo again.")

    async def handler(_args):
        try:
            return await asyncio.to_thread(faces.enroll, name, image_bytes)
        except ValueError as exc:
            return f"Couldn't enroll that face: {exc}"

    return await orchestrator._gated(
        chat_id, SkillCall("faces.enroll", {"name": name}), handler,
        describe=(f'About to store a FACE TEMPLATE for "{name}" — biometric '
                  "data, kept ONLY on this machine (never sent anywhere), "
                  f'deletable anytime with "forget the face {name}"'))


async def _on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo becomes one frontier vision call — analysis only, no tools
    on this path (see agents/photo.py). The reply plus a text record land
    in history so follow-up questions work."""
    if not _owner_private(update):
        return
    import base64

    from kyraan.agents import orchestrator, photo
    from kyraan.control_plane.logging_setup import log_trace, new_turn

    from kyraan.agents import faces

    new_turn()
    chat_id = update.effective_chat.id
    caption = update.message.caption or ""
    log_trace("turn_start", chat_id=chat_id, user_text=f"[photo] {caption}")
    typing = asyncio.create_task(_typing_loop(context.bot, chat_id))
    try:
        # largest thumbnail Telegram offers — plenty for detail=low vision
        from kyraan.control_plane.logging_setup import stage as _stage
        with _stage("photo_download"):
            tg_file = await update.message.photo[-1].get_file()
            image_bytes = bytes(await tg_file.download_as_bytearray())
        faces.stash_photo(chat_id, image_bytes)

        # Either form: the strict phrase ("remember this face as X") or
        # the natural one ("remember this is Suman Ghosh") — with the
        # photo in the same message the intent is unambiguous, and the
        # confirm gate still stands (seen live 2026-08-26 23:08: the
        # natural caption described the photo instead of enrolling).
        enroll_name = faces.enroll_request(caption) or faces.enroll_from_text(caption)
        if enroll_name is None and not caption.strip():
            # The bot's own last reply may have ASKED for this photo
            # ("send a photo to save Kamal's face") — a captionless photo
            # right after is the answer, not a random picture; the
            # confirm gate below still owns the biometric write.
            recent = [t for role, t in orchestrator._history[chat_id]
                      if role == "assistant"]
            if recent:
                enroll_name = faces.invite_followup(recent[-1])
        if enroll_name is not None:
            # Biometric write → the standard confirm gate; the photo's
            # bytes stay captured in the handler for the owner's yes.
            reply = await _enroll_face_gated(chat_id, enroll_name, image_bytes)
            orchestrator.record_exchange(chat_id, f"[sent a photo: {caption}]", reply)
            from kyraan.control_plane.logging_setup import turn_summary
            log_trace("turn_end", chat_id=chat_id, reply=reply, **turn_summary())
            await update.message.reply_text(_plain(reply), do_quote=True)
            return

        with _stage("face_recognize"):
            recognized = (await asyncio.to_thread(faces.recognize, image_bytes)
                          if faces.available()
                          else {"names": [], "maybe": [], "unknown_faces": 0})
        data_url = ("data:image/jpeg;base64,"
                    + base64.b64encode(image_bytes).decode())
        reply, vision_enroll = await photo.answer(
            chat_id, data_url, caption,
            recognized=recognized["names"],
            maybe=recognized.get("maybe") or [])
        if vision_enroll and faces.available():
            # The vision model read enrollment intent in the caption (any
            # wording) — the regex above only catches the fixed phrases.
            # Same confirm gate; the ask replaces the descriptive reply.
            reply = await _enroll_face_gated(chat_id, vision_enroll, image_bytes)
            orchestrator.record_exchange(
                chat_id, f"[sent a photo: {caption}]", reply)
            log_trace("turn_end", chat_id=chat_id, reply=reply)
            await update.message.reply_text(
                _plain(reply), do_quote=True,
                reply_markup=_confirm_keyboard(chat_id))
            return
        if (faces.available()
                and re.search(r"who(?:'s| is)|do you (?:know|recogni[sz]e)",
                              caption, re.IGNORECASE)
                and not recognized["names"] and not recognized.get("maybe")):
            # A who-question with NO match answers with the truth about
            # what IS saved — live 2026-08-28: a no-match got a plain
            # visual description and the owner had to interrogate why
            # ("you said suman's face record but you can't recognise").
            enrolled = faces.enrolled_names()
            reply += ("\n\n(Checked against my saved faces — no match. "
                      + (f"I have face data for: {', '.join(enrolled)}."
                         if enrolled else "No faces are enrolled yet.")
                      + ")")
        hint_name = faces.enroll_hint(caption) if faces.available() else None
        if hint_name:
            reply += (f'\n\n(Want me to recognize this face later? Send a solo '
                      f'photo of them captioned "remember this face as '
                      f'{hint_name}" — face data stays on this machine.)')
    except photo.VisionUnavailable:
        reply = ("I can't see photos right now (the vision model is "
                 "unavailable) — tell me in words for now.")
    except Exception as exc:
        logger.warning("Photo handling failed: %s", exc)
        reply = "Something went wrong with that photo — try sending it again."
    finally:
        typing.cancel()
    orchestrator.record_exchange(
        update.effective_chat.id,
        f"[sent a photo{': ' + caption if caption else ''}]", reply)
    from kyraan.control_plane.logging_setup import turn_summary
    log_trace("turn_end", chat_id=update.effective_chat.id, reply=reply,
              **turn_summary())
    await update.message.reply_text(_plain(reply), do_quote=True)


_PDF_MAX_BYTES = 15 * 1024 * 1024


async def _on_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Document memory (2026-08-27): a PDF from the OWNER is captured —
    text layer extracted locally (no model, no cloud), chunked, embedded
    locally, stored. Scanned PDFs without a text layer get an honest
    'can't read scans yet'. Owner-only like all media until P3.5b/c
    scope media for other stages."""
    if not _owner_private(update):
        return
    document = update.message.document
    chat_id = update.effective_chat.id
    if (document.file_size or 0) > _PDF_MAX_BYTES:
        await update.message.reply_text(
            "That PDF is over 15MB — too big for me to take in.")
        return
    try:
        handle = await document.get_file()
        data = bytes(await handle.download_as_bytearray())
        import io

        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as exc:
        logger.warning("pdf capture failed: %s", exc)
        await update.message.reply_text(
            "I couldn't read that PDF — it may be corrupted or protected.")
        return
    if len(text) < 20:
        # A scan: no text layer. OCR the embedded page images through
        # the vision tier (owner gap list 2026-08-27), first 4 pages.
        import base64

        from kyraan.agents import photo as _photo
        pieces = []
        for page in reader.pages[:4]:
            try:
                images = page.images
            except Exception:
                images = []
            for img in images[:1]:
                data_url = ("data:image/jpeg;base64,"
                            + base64.b64encode(img.data).decode())
                piece = await _photo.transcribe(data_url)
                if piece:
                    pieces.append(piece)
                break
        text = "\n\n".join(pieces).strip()
        if len(text) < 20:
            await update.message.reply_text(
                "That PDF looks scanned and I couldn't read its pages — "
                "a clear photo of the page works better.")
            return
    from kyraan.store import documents
    import asyncio as _aio
    doc_id = await _aio.to_thread(
        documents.ingest, chat_id, "pdf", text,
        (update.message.caption or "")[:120], document.file_name or "")
    reply = (f'📄 Saved "{document.file_name}" to document memory '
             f"({len(reader.pages)} page{'s' if len(reader.pages) != 1 else ''}, "
             f"{len(text):,} chars) — ask me about it anytime."
             if doc_id else
             "I couldn't distill any text worth keeping from that PDF.")
    orchestrator.record_exchange(
        chat_id, f"[sent a PDF: {document.file_name}]", reply)
    await update.message.reply_text(reply, do_quote=True)


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


async def _agent_task_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    from kyraan.triggers import agent_tasks
    await agent_tasks.fire(context.job.data["task_id"])


def _wire_agent_tasks(job_queue: JobQueue, bot) -> None:
    from kyraan.agents import agent_loop
    from kyraan.triggers import agent_tasks

    def schedule_fn(job_name: str, run_at, payload: dict) -> None:
        job_queue.run_once(_agent_task_job, when=run_at, data=payload, name=job_name)

    async def run_fn(chat_id: int, instruction: str) -> str:
        for tier in ("frontier", "cheap"):
            try:
                return await agent_loop.run(chat_id, instruction, tier=tier, read_only=True)
            except agent_loop.AgentUnavailable:
                continue
        return ""

    async def send_fn(chat_id: int, text: str) -> None:
        if chat_id != _owner_id():
            return
        await bot.send_message(chat_id=chat_id, text=_plain(text))
        orchestrator.record_proactive(chat_id, text)

    agent_tasks.init(schedule_fn=schedule_fn, run_fn=run_fn, send_fn=send_fn,
                     only_chat=_owner_id())


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
    async def _send(context, chat_id: int, text: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=text)
        orchestrator.record_proactive(chat_id, text)

    at = briefs.brief_time("morning")
    if at is not None:
        async def _morning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
            await briefs.fire(_owner_id(), lambda c, t: _send(context, c, t))

        # run_daily needs a tz-aware time or it fires in UTC.
        job_queue.run_daily(_morning_job, time=at.replace(tzinfo=local_now().tzinfo),
                            name="morning_brief")
        logger.info("Morning brief scheduled daily at %s %s", at, local_now().tzinfo)

    evening_at = briefs.brief_time("evening")
    if evening_at is not None:
        async def _evening_job(context: ContextTypes.DEFAULT_TYPE) -> None:
            await briefs.fire_evening(_owner_id(), lambda c, t: _send(context, c, t))

        job_queue.run_daily(_evening_job,
                            time=evening_at.replace(tzinfo=local_now().tzinfo),
                            name="evening_brief")
        logger.info("Evening brief scheduled daily at %s", evening_at)

    review_at = __import__("kyraan.triggers.self_review", fromlist=["x"]).review_time()
    if review_at is not None:
        from kyraan.triggers import self_review

        async def _review_job(context: ContextTypes.DEFAULT_TYPE) -> None:
            # Every nightly stage runs INDEPENDENTLY. They were chained,
            # so a failure in the critique or the episode ingest skipped
            # everything after it — including the forget re-sweep, which
            # is what keeps forgotten topics from lingering findable in
            # new episodes. A privacy repair must not depend on an
            # unrelated step succeeding (Bugbot P1).
            import asyncio as _aio
            from datetime import timedelta as _td

            from kyraan.control_plane.dnd import local_now as _now

            async def _stage(name: str, run):
                try:
                    await run()
                except Exception as exc:
                    logger.exception("nightly stage %s failed", name)
                    log_event("nightly_stage_failed", stage=name,
                              error=str(exc)[:200],
                              error_type=type(exc).__name__)

            await _stage("self_review", lambda: self_review.fire(
                _owner_id(), lambda c, t: _send(context, c, t)))

            # P3.3b: the nightly episode write rides the same job —
            # yesterday+today because ingest is idempotent and last
            # night's run stopped at this hour. Blocking work (local
            # embed + tagging + pg) stays off the event loop.
            async def _ingest():
                from kyraan.store import episodes as _episodes
                today = _now().date()
                days = [(today - _td(days=1)).isoformat(), today.isoformat()]
                await _aio.to_thread(_episodes.ingest_recent, days)

            await _stage("episode_ingest", _ingest)

            # The self-heals ride along nightly (gap audit 2026-08-27:
            # they only ran on a manual resync). Separate stages: the
            # forget re-sweep is a privacy repair and the graph catch-up
            # is bookkeeping — neither may block the other.
            async def _resweep():
                from kyraan.memory import engine as _engine
                await _aio.to_thread(_engine.resweep_forgotten)

            async def _graph_catch_up():
                from kyraan.store import triples as _triples
                await _aio.to_thread(_triples.catch_up)

            await _stage("forget_resweep", _resweep)
            await _stage("graph_catch_up", _graph_catch_up)

            # P3.5e: expired 24h objection windows promote nightly.
            async def _auto_approvals():
                from kyraan.memory import review_scaling as _scaling
                await _aio.to_thread(_scaling.sweep_auto_approvals)

            await _stage("auto_approvals", _auto_approvals)

            # Cross-person contradiction scan (multi-user audit fix):
            # undeclared conflicts become the standard dispute state —
            # both facts flagged, a resolvable notice in the right queue.
            async def _conflict_scan():
                from kyraan.control_plane import kernel as _kernel
                from kyraan.memory import conflicts as _conflicts
                filed = await _aio.to_thread(_conflicts.nightly_scan)
                if filed and _kernel.can_send_proactively():
                    await _send(context, _owner_id(),
                                f"⚖️ Memory: {filed} cross-person "
                                "contradiction(s) found — both versions "
                                'stand as disputed; say "review memory" '
                                "to resolve.")

            await _stage("conflict_scan", _conflict_scan)

            # The nightly doctor: silent when healthy; WARN/FAIL sends
            # the owner the needs-work list (DND-gated like every
            # proactive).
            async def _health_report():
                from kyraan.control_plane import health as _health
                from kyraan.control_plane import kernel as _kernel
                verdict, text = await _aio.to_thread(_health.report)
                if verdict != "OK" and _kernel.can_send_proactively():
                    await _send(context, _owner_id(),
                                f"🩺 Nightly health: {verdict}\n{text}")

            await _stage("health_report", _health_report)

            # Semantic dedup scan (owner: "make it automate",
            # 2026-08-27): the model PROPOSES nightly; applying stays
            # behind the owner's yes via the "consolidate memory" chat
            # phrase. DND-gated like every proactive.
            async def _dedup_scan():
                from kyraan.control_plane import kernel as _kernel
                from kyraan.memory import consolidate as _consolidate
                proposals = await _aio.to_thread(_consolidate.scan)
                if proposals and _kernel.can_send_proactively():
                    lines = [f'• keep "{p["keep_content"][:70]}" over '
                             + "; ".join(f'"{c[:60]}"' for _, c in p["duplicates"])
                             for p in proposals]
                    await _send(context, _owner_id(),
                                "🧹 Memory dedup: "
                                f"{len(proposals)} duplicate group(s) found:\n"
                                + "\n".join(lines)
                                + '\n\nSay "consolidate memory" to review and apply.')

            await _stage("memory_dedup_scan", _dedup_scan)

        job_queue.run_daily(_review_job,
                            time=review_at.replace(tzinfo=local_now().tzinfo),
                            name="self_review")
        logger.info("Nightly self-review scheduled at %s", review_at)

    from kyraan.triggers import home_alerts
    if home_alerts.enabled():
        async def _alerts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
            await home_alerts.check(_owner_id(), lambda c, t: _send(context, c, t))

        job_queue.run_repeating(_alerts_job, interval=1800, first=120,
                                name="home_alerts")
        logger.info("Home alerts armed (every 30 min, DND-gated)")

    # Event-triggered watch rules (owner, 2026-08-27): user-defined
    # conditions evaluated every 15 min — notify-only by doctrine.
    from kyraan.triggers import event_rules

    async def _rules_job(context: ContextTypes.DEFAULT_TYPE) -> None:
        # Send via the job's context like every proactive job here — a
        # module-level send_fn captured at wiring time referenced a name
        # not in this scope and every tick died with NameError (found
        # live: the owner's first rule never fired at 27.4°C).
        await event_rules.tick(lambda c, t: _send(context, c, t))

    job_queue.run_repeating(_rules_job, interval=900, first=180,
                            name="event_rules")
    logger.info("Watch rules armed (every 15 min, DND-gated)")


def _harden_data_permissions() -> None:
    """Personal data is owner-only on disk (security round P2: 0644/0755
    let any local account read the chat log and memory tree)."""
    import stat
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    for name in ("logs", "memory", "data"):
        root = repo / name
        if not root.exists():
            continue
        os.chmod(root, 0o700)
        for path in root.rglob("*"):
            try:
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
            except OSError:
                pass


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
        if (kind in ("openai_compatible", "ollama_native")
                and not provider.get("api_key_env")
                and provider.get("allow_unauthenticated") is not True):
            # Keyless must be DECLARED, not inferred from the hostname
            # (round-6 P2: a local authenticated proxy broke the implicit
            # localhost-is-keyless assumption silently).
            # `is True` on purpose: "false" (string) and 1 are truthy —
            # a mistyped security flag must not grant the bypass (round-7).
            raise ValueError(
                f"provider {pname!r} has no api_key_env — if that is intentional "
                "(local unauthenticated server), set allow_unauthenticated: true "
                "(Boolean true exactly)")
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
    _harden_data_permissions()
    from kyraan.channels import voice as _voice_probe
    _voice_probe.start_probe()  # verdict ready before the first voice note
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler(["start", "help"], _on_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    app.add_handler(MessageHandler(filters.VOICE, _on_voice))
    app.add_handler(MessageHandler(filters.LOCATION | filters.VENUE, _on_location))
    app.add_handler(MessageHandler(filters.PHOTO, _on_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, _on_pdf))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.AUDIO
        | filters.Sticker.ALL | filters.Document.ALL,
        _on_unsupported,
    ))
    app.add_handler(CallbackQueryHandler(_on_callback, pattern="^kyraan_(yes|no)"))

    _wire_scheduler(app.job_queue, app.bot)
    _wire_agent_tasks(app.job_queue, app.bot)
    _wire_brief(app.job_queue, app.bot)
    # A restart must be invisible to the owner: reload the conversation
    # from chat.jsonl so follow-ups ("are those the latest emails?") still
    # have their context.
    orchestrator.seed_history_from_log()
    from kyraan.memory import engine
    engine.migrate_from_tree()  # one-time index backfill; no-op after

    logger.info("Kyraan Telegram bot starting (owner-only, Phase 1)")
    app.run_polling()
