import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from nanoclaw.agent import run_agent, clear_session_id
from nanoclaw.conversations import archive_exchange, get_recent_history
from nanoclaw.config import ASSISTANT_NAME, DB_PATH, OWNER_ID, TELEGRAM_BOT_TOKEN
from nanoclaw.scheduler import setup_scheduler

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LENGTH = 4096
_AGENT_TIMEOUT = 300  # seconds — hard limit for agent response
_TYPING_INTERVAL = 4  # seconds — Telegram typing status lasts ~5s

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


async def _handle_message(update: Update, context) -> None:
    if not _is_owner(update) or not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text

    # Inject active task info into prompt so the agent is aware
    active = _active_tasks.get(chat_id)
    if active:
        user_text = f"[시스템: 현재 다른 작업이 병렬로 진행 중입니다 — \"{active}\"]\n\n{user_text}"

    _active_tasks[chat_id] = update.message.text[:60]

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id, stop_typing))

    # Short messages likely don't need much context; longer ones may reference past conversation
    history_size = 3 if len(update.message.text) < 30 else 10
    history = get_recent_history(max_exchanges=history_size)

    try:
        response = await asyncio.wait_for(
            run_agent(user_text, context.bot, chat_id, str(DB_PATH), history=history),
            timeout=_AGENT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        response = "⏱ 응답 시간이 초과되었어요. 좀 더 간단하게 다시 질문해주세요."
        logger.warning("Agent timed out after %ds for: %s", _AGENT_TIMEOUT, user_text[:100])
    finally:
        _active_tasks.pop(chat_id, None)
        stop_typing.set()
        await typing_task

    # Archive to conversations/ for long-term memory
    await archive_exchange(user_text, response, chat_id)

    # Always send final response
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
    return app
