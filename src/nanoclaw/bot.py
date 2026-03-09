import asyncio
import base64
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from nanoclaw.agent import run_agent, clear_session_id
from nanoclaw.conversations import archive_exchange, get_recent_history
from nanoclaw.config import AGENT_TIMEOUT, ASSISTANT_NAME, DB_PATH, OWNER_ID, TELEGRAM_BOT_TOKEN
from nanoclaw.rewriter import rewrite_on_timeout
from nanoclaw.scheduler import setup_scheduler

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LENGTH = 4096
_continue_futures: dict[int, asyncio.Future] = {}  # chat_id -> Future[bool]
_auto_continue: dict[int, int] = {}  # chat_id -> remaining auto-retries
_AGENT_TIMEOUT = AGENT_TIMEOUT
_MAX_AUTO_RETRIES = 3
_TYPING_INTERVAL = 4  # seconds — Telegram typing status lasts ~5s
_PROGRESS_INTERVAL = 30  # seconds — send progress update if agent is silent

# Track active tasks per chat: {chat_id: "description of current task"}
_active_tasks: dict[int, str] = {}


def _is_owner(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == OWNER_ID


async def _start(update: Update, context) -> None:
    if not _is_owner(update):
        return
    await update.message.reply_text(
        f"Hi! I'm {ASSISTANT_NAME}, your personal AI assistant. Send me a message to get started.\n\n"
        "Commands:\n"
        "/clear - Reset conversation session"
    )


async def _clear(update: Update, context) -> None:
    if not _is_owner(update):
        return
    clear_session_id()
    await update.message.reply_text("Session cleared. Starting fresh!")


async def _keep_typing(bot, chat_id: int, stop: asyncio.Event) -> None:
    """Send typing indicator every few seconds until stopped."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=_TYPING_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


async def _report_progress(bot, chat_id: int, stop: asyncio.Event,
                           progress: dict, notify_state: dict) -> None:
    """Periodically send progress updates when agent is silent for too long."""
    last_reported = ""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_PROGRESS_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass
        # Skip if agent already sent a message to the user recently
        if notify_state.get("sent"):
            continue
        current = progress.get("last_text", "")
        if current and current != last_reported:
            preview = current[:200].strip()
            if len(current) > 200:
                preview += "..."
            try:
                await bot.send_message(chat_id=chat_id, text=f"⏳ 진행중:\n{preview}")
            except Exception:
                pass
            last_reported = current
        elif not current:
            try:
                await bot.send_message(chat_id=chat_id, text="⏳ 처리 중...")
            except Exception:
                pass


async def _handle_continue(update: Update, context) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    labels = {"continue_yes": "Continued ▶", "continue_no": "Stopped ■", "continue_auto": "Auto-continued ▶▶"}
    chosen = labels.get(query.data, query.data)
    await query.edit_message_text(f"{query.message.text}\n\n→ {chosen}")
    if query.data == "continue_auto":
        _auto_continue[chat_id] = _MAX_AUTO_RETRIES
    fut = _continue_futures.pop(chat_id, None)
    if fut and not fut.done():
        fut.set_result(query.data in ("continue_yes", "continue_auto"))


async def _handle_message(update: Update, context) -> None:
    if not _is_owner(update) or not update.message:
        return

    # Extract text and optional photo
    user_text = update.message.text or update.message.caption or ""
    images: list[dict] = []

    if update.message.photo:
        # Get the largest photo (last in the list)
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        data = await file.download_as_bytearray()
        images.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(bytes(data)).decode(),
            },
        })
        if not user_text:
            user_text = "(사진)"

    if not user_text and not images:
        return

    chat_id = update.effective_chat.id

    # Inject active task info into prompt so the agent is aware
    active = _active_tasks.get(chat_id)
    if active:
        user_text = f"[System: Another task is running in parallel — \"{active}\"]\n\n{user_text}"

    _active_tasks[chat_id] = user_text[:60]

    stop_typing = asyncio.Event()
    stop_progress = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id, stop_typing))

    # Short messages likely don't need much context; longer ones may reference past conversation
    history_size = 3 if len(user_text) < 30 else 10
    history = get_recent_history(max_exchanges=history_size)

    response = ""
    notify_state: dict = {"sent": False, "messages": []}
    progress: dict = {"last_text": "", "all_texts": []}
    progress_task = asyncio.create_task(
        _report_progress(context.bot, chat_id, stop_progress, progress, notify_state)
    )

    attempt = 0
    try:
        while True:
            attempt += 1
            if attempt == 1:
                prompt = user_text
            else:
                prompt = await rewrite_on_timeout(
                    original_prompt=user_text,
                    work_done=progress.get("all_texts", []),
                    messages_sent=notify_state.get("messages", []),
                    attempt=attempt,
                )
                # Reset progress for new attempt (keep notify_state across attempts)
                progress = {"last_text": "", "all_texts": []}
            try:
                agent_task = asyncio.create_task(
                    run_agent(prompt, context.bot, chat_id, str(DB_PATH),
                              history=history, notify_state=notify_state, progress=progress,
                              reply_to_message_id=update.message.message_id,
                              images=images)
                )
                done, _ = await asyncio.wait({agent_task}, timeout=_AGENT_TIMEOUT)
                if done:
                    response = agent_task.result()
                    if notify_state.get("max_turns_exhausted"):
                        # Agent hit max_turns — treat like timeout for retry
                        logger.warning("Agent exhausted max_turns (attempt %d)", attempt)
                        notify_state["max_turns_exhausted"] = False
                        raise asyncio.TimeoutError()
                    break  # success
                else:
                    # Timeout — cancel and give agent a grace period to clean up
                    agent_task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(agent_task), timeout=5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                    raise asyncio.TimeoutError()
            except asyncio.TimeoutError:
                logger.warning("Agent timed out (attempt %d) for: %s", attempt, user_text[:100])
                stop_typing.set()
                stop_progress.set()
                await typing_task
                await progress_task

                # Check auto-continue
                remaining = _auto_continue.get(chat_id, 0)
                if remaining > 0:
                    _auto_continue[chat_id] = remaining - 1
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⏱ Timed out (attempt {attempt}) — \"{user_text[:50]}\" — auto-retrying ({remaining} left)...",
                    )
                    should_continue = True
                else:
                    _auto_continue.pop(chat_id, None)
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Continue", callback_data="continue_yes"),
                         InlineKeyboardButton("Don't ask", callback_data="continue_auto"),
                         InlineKeyboardButton("Stop", callback_data="continue_no")],
                    ])
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⏱ Timed out (attempt {attempt}) — \"{user_text[:50]}\" — retry?",
                        reply_markup=keyboard,
                    )

                    fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                    _continue_futures[chat_id] = fut
                    should_continue = await fut

                if not should_continue:
                    if notify_state.get("messages"):
                        response = ""
                    else:
                        response = "⏱ Timed out. No results to show."
                    break

                # Resume typing and progress reporting for next attempt
                stop_typing = asyncio.Event()
                stop_progress = asyncio.Event()
                typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id, stop_typing))
                progress_task = asyncio.create_task(
                    _report_progress(context.bot, chat_id, stop_progress, progress, notify_state)
                )
    finally:
        _active_tasks.pop(chat_id, None)
        _auto_continue.pop(chat_id, None)
        stop_typing.set()
        stop_progress.set()
        await typing_task
        await progress_task

    # Archive to conversations/ for long-term memory
    await archive_exchange(user_text, response, chat_id)

    logger.info("Response ready for: %s", user_text[:80])

    # Send final response (skip if agent already sent via send_message tool)
    if response:
        for i in range(0, len(response), _TELEGRAM_MAX_LENGTH):
            chunk = response[i : i + _TELEGRAM_MAX_LENGTH]
            await context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=update.message.message_id,
            )


async def _post_init(application: Application) -> None:
    scheduler = setup_scheduler(application.bot, str(DB_PATH))
    scheduler.start()
    logger.info("Scheduler started")


def setup_bot() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("clear", _clear))
    app.add_handler(CallbackQueryHandler(_handle_continue, pattern="^continue_"))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, _handle_message))
    return app
