import asyncio
import base64
import logging
import os

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from nanoclaw.agent import run_agent, clear_session_id, set_telegram_bot
from nanoclaw.conversations import archive_exchange, get_recent_history
from nanoclaw.config import AGENT_TIMEOUT, ASSISTANT_NAME, DB_PATH, OWNER_ID, TELEGRAM_BOT_TOKEN
from nanoclaw.rewriter import rewrite_on_timeout

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_LENGTH = 4096
_AGENT_TIMEOUT = AGENT_TIMEOUT
_MAX_RETRIES = 3
_TYPING_INTERVAL = 4  # seconds — Telegram typing status lasts ~5s
_PROGRESS_INTERVAL = 60  # seconds — send progress update if agent is silent

# Track active tasks per chat: {chat_id: (description, agent_task, cancel_event, stop_typing, stop_progress)}
_active_tasks: dict[int, tuple[str, asyncio.Task, asyncio.Event, asyncio.Event, asyncio.Event]] = {}


def _kill_agent_subprocesses() -> None:
    """Kill claude.exe subprocesses spawned by the SDK under this nanoclaw process tree."""
    try:
        import psutil
        # Walk up to the root nanoclaw.exe, then kill all claude.exe descendants
        me = psutil.Process()
        root = me
        while root.parent() and "nanoclaw" in root.parent().name().lower():
            root = root.parent()
        for child in root.children(recursive=True):
            try:
                name = child.name().lower()
                if "claude" in name:
                    logger.info("Killing subprocess PID %d (%s) + children", child.pid, child.name())
                    for grandchild in child.children(recursive=True):
                        grandchild.kill()
                    child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        logger.exception("Error killing agent subprocesses")


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
        # Save image to workspace so the agent can process it with tools
        from nanoclaw.config import WORKSPACE_DIR
        uploads_dir = WORKSPACE_DIR / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        img_filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{photo.file_id[:8]}.jpg"
        img_path = uploads_dir / img_filename
        img_path.write_bytes(bytes(data))
        img_path_str = str(img_path.resolve())
        if not user_text:
            user_text = "(사진)"
        user_text += f"\n\n[이미지 파일 경로: {img_path_str}]"

    if not user_text and not images:
        return

    chat_id = update.effective_chat.id

    # Cancel running task if user sends "취소"
    if user_text.strip() in ("취소", "cancel"):
        active = _active_tasks.get(chat_id)
        if active:
            desc, task, cancel_ev, stop_typ, stop_prog = active
            cancel_ev.set()
            stop_typ.set()
            stop_prog.set()
            _kill_agent_subprocesses()
            task.cancel()
            await update.message.reply_text(f"작업을 취소했습니다: \"{desc}\"")
        else:
            await update.message.reply_text("진행 중인 작업이 없습니다.")
        return

    # Inject active task info into prompt so the agent is aware
    active = _active_tasks.get(chat_id)
    if active:
        desc, _, _, _, _ = active
        user_text = f"[System: Another task is running in parallel — \"{desc}\"]\n\n{user_text}"

    cancel_event = asyncio.Event()
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
            if cancel_event.is_set():
                logger.info("Task cancelled by user: %s", user_text[:80])
                response = ""
                break
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
                _active_tasks[chat_id] = (user_text[:60], agent_task, cancel_event, stop_typing, stop_progress)
                cancel_waiter = asyncio.create_task(cancel_event.wait())
                done, pending = await asyncio.wait(
                    {agent_task, cancel_waiter},
                    timeout=_AGENT_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                cancel_waiter.cancel()
                if cancel_event.is_set():
                    agent_task.cancel()
                    _kill_agent_subprocesses()
                    try:
                        await asyncio.wait_for(asyncio.shield(agent_task), timeout=3)
                    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                        pass
                    response = ""
                    break
                if agent_task in done:
                    response = agent_task.result()
                    if notify_state.get("max_turns_exhausted"):
                        logger.warning("Agent exhausted max_turns (attempt %d)", attempt)
                        notify_state["max_turns_exhausted"] = False
                        if notify_state.get("messages"):
                            response = ""
                            break
                        raise asyncio.TimeoutError()
                    break  # success
                else:
                    # Timeout — cancel agent task
                    agent_task.cancel()
                    try:
                        await agent_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    raise asyncio.TimeoutError()
            except asyncio.TimeoutError:
                logger.warning("Agent timed out (attempt %d) for: %s", attempt, user_text[:100])
                stop_typing.set()
                stop_progress.set()
                await typing_task
                await progress_task

                if attempt >= _MAX_RETRIES:
                    if notify_state.get("messages"):
                        response = ""
                    else:
                        response = f"⏱ Timed out after {attempt} attempts. No results to show."
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⏱ Timed out (attempt {attempt}/{_MAX_RETRIES}) — 중단합니다.",
                    )
                    break

                remaining = _MAX_RETRIES - attempt
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏱ Timed out (attempt {attempt}/{_MAX_RETRIES}) — auto-retrying ({remaining} left)...",
                )

                # Resume typing and progress reporting for next attempt
                stop_typing = asyncio.Event()
                stop_progress = asyncio.Event()
                typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id, stop_typing))
                progress_task = asyncio.create_task(
                    _report_progress(context.bot, chat_id, stop_progress, progress, notify_state)
                )
    finally:
        _active_tasks.pop(chat_id, None)
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


def setup_bot() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("clear", _clear))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, _handle_message))
    return app
