import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)
from croniter import croniter

from nanoclaw import db
from nanoclaw.config import (
    AGENT_TIMEOUT,
    ANTHROPIC_API_KEY,
    ANTHROPIC_BASE_URL,
    LOCAL_TZ,
    STATE_FILE,
    WORKSPACE_DIR,
)

logger = logging.getLogger(__name__)

_MAX_TURNS = 30

# Telegram bot reference for cross-channel send_message
_telegram_bot: Any = None
_telegram_chat_id: int = 0


_CODEX_TIMEOUT_S = 480  # codex exec wall time budget per image (gpt-image can take 2min+)

# Keep strong refs to background image tasks so asyncio doesn't GC them while running.
_BG_IMAGE_TASKS: set[asyncio.Task] = set()


async def _bg_generate_and_deliver(
    description: str,
    caption: str | None,
    bot: Any,
    target_chat_id: int,
    reply_to_message_id: int | None,
) -> None:
    """Run codex image gen in the background, then deliver via Telegram bot."""
    logger.info("bg_image: start chat=%s prompt=%r", target_chat_id, description[:80])
    path_str, err = await _codex_generate_image(description)
    try:
        if err or not path_str:
            logger.error("bg_image: generation failed: %s", err)
            await bot.send_message(
                chat_id=target_chat_id,
                text=f"❌ 이미지 생성 실패: {err}",
            )
            return
        path = Path(path_str)
        kwargs: dict[str, Any] = {"chat_id": target_chat_id}
        if caption:
            kwargs["caption"] = caption
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        try:
            with path.open("rb") as f:
                await bot.send_photo(photo=f, **kwargs)
        except Exception:
            logger.exception("bg_image: send_photo with reply_to failed, retrying without")
            kwargs.pop("reply_to_message_id", None)
            with path.open("rb") as f:
                await bot.send_photo(photo=f, **kwargs)
        logger.info("bg_image: delivered %s", path.name)
    except Exception:
        logger.exception("bg_image: unexpected failure")
        try:
            await bot.send_message(chat_id=target_chat_id, text="❌ 이미지 전송 중 예기치 못한 오류.")
        except Exception:
            logger.exception("bg_image: failed to even notify user of failure")


async def _codex_generate_image(description: str) -> tuple[str | None, str]:
    """Run `codex exec $imagegen` and save PNG to workspace.

    Returns (absolute_path, "") on success, or (None, error_message) on failure.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = str((WORKSPACE_DIR / f"gen_{timestamp}.png").resolve())
    logger.info("codex_generate: start prompt=%r output=%s", description[:80], output_path)
    codex_prompt = (
        f"Use the image generation skill (invoke $imagegen) to create this image: {description}\n\n"
        f"Save the generated image as {output_path}, overwriting any existing file at that path. "
        f"Do not write any other files or source code. Only produce the image."
    )
    codex_argv = _resolve_codex_argv()
    if not codex_argv:
        return None, "codex CLI not found (tried CODEX_BIN env, C:\\nvm4w\\nodejs, PATH)"
    try:
        proc = await asyncio.create_subprocess_exec(
            *codex_argv, "exec", "--full-auto", "--skip-git-repo-check", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return None, f"failed to launch codex: {e}"
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=codex_prompt.encode("utf-8")),
            timeout=_CODEX_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, f"codex exec timed out after {_CODEX_TIMEOUT_S}s"
    if proc.returncode != 0:
        tail = (stderr_b or stdout_b).decode("utf-8", "replace")[-500:]
        return None, f"codex exec failed (exit {proc.returncode}): {tail}"
    if not os.path.exists(output_path):
        tail = stdout_b.decode("utf-8", "replace")[-500:]
        logger.error("codex_generate: file missing, tail=%r", tail[-200:])
        return None, f"image file not created at {output_path}. codex tail:\n{tail}"
    size = os.path.getsize(output_path)
    logger.info("codex_generate: done %s (%d bytes)", output_path, size)
    return output_path, ""


def _resolve_codex_argv() -> list[str] | None:
    """Locate the codex CLI as an absolute-path argv suitable for subprocess_exec.

    asyncio.create_subprocess_exec on Windows cannot invoke .cmd/.bat shims,
    so we resolve to node.exe + codex.js directly.
    """
    override = os.environ.get("CODEX_BIN")
    if override and os.path.exists(override):
        return [override]
    node = r"C:\nvm4w\nodejs\node.exe"
    codex_js = r"C:\nvm4w\nodejs\node_modules\@openai\codex\bin\codex.js"
    if os.path.exists(node) and os.path.exists(codex_js):
        return [node, codex_js]
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found and found.lower().endswith(".exe"):
        return [found]
    return None


def set_telegram_bot(bot: Any, chat_id: int) -> None:
    """Store Telegram bot reference so send_message always reaches Telegram."""
    global _telegram_bot, _telegram_chat_id
    _telegram_bot = bot
    _telegram_chat_id = chat_id

# Patch SDK to ignore unknown message types (e.g. rate_limit_event) instead of crashing
def _patch_message_parser():
    try:
        from claude_agent_sdk._internal import message_parser, client as _sdk_client
        _original = message_parser.parse_message
        def _patched(data):
            try:
                return _original(data)
            except message_parser.MessageParseError as e:
                if "Unknown message type" in str(e):
                    logger.debug("Ignoring unknown message type: %s", data.get("type"))
                    return None
                raise
        message_parser.parse_message = _patched
        _sdk_client.parse_message = _patched  # client.py imports parse_message directly
    except Exception:
        logger.debug("Could not patch message_parser", exc_info=True)

_patch_message_parser()


def _create_tools(bot: Any, chat_id: int, db_path: str, notify_state: dict | None = None, reply_to_message_id: int | None = None) -> list:
    @tool("send_message", "Send a message to the user on Telegram", {"text": str})
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"chat_id": chat_id or _telegram_chat_id, "text": args["text"]}
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        await bot.send_message(**kwargs)
        if notify_state is not None:
            notify_state["sent"] = True
            notify_state.setdefault("messages", []).append(args["text"])
        return {"content": [{"type": "text", "text": "Message sent."}]}

    @tool(
        "schedule_task",
        "Schedule a task. schedule_type: 'cron', 'interval', or 'once'. schedule_value: cron expression, milliseconds, or ISO timestamp.",
        {"prompt": str, "schedule_type": str, "schedule_value": str},
    )
    async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
        stype = args["schedule_type"]
        svalue = args["schedule_value"]
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(LOCAL_TZ)

        if stype == "cron":
            # Cron expressions are interpreted in local time
            next_local = croniter(svalue, now_local).get_next(datetime)
            next_run = next_local.astimezone(timezone.utc).isoformat()
        elif stype == "interval":
            next_run = (now_utc + timedelta(milliseconds=int(svalue))).isoformat()
        elif stype == "once":
            dt = datetime.fromisoformat(svalue)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            next_run = dt.astimezone(timezone.utc).isoformat()
        else:
            return {
                "content": [{"type": "text", "text": f"Unknown schedule_type: {stype}"}],
                "is_error": True,
            }

        task_id = await db.create_task(db_path, chat_id, args["prompt"], stype, svalue, next_run)
        local_next = datetime.fromisoformat(next_run).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Task {task_id} scheduled. Next run: {local_next}",
                }
            ]
        }

    @tool("list_tasks", "List all scheduled tasks", {})
    async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        tasks = await db.get_all_tasks(db_path)
        if not tasks:
            return {"content": [{"type": "text", "text": "No scheduled tasks."}]}
        lines = []
        for t in tasks:
            next_run = t.get("next_run", "")
            if next_run:
                next_run = datetime.fromisoformat(next_run).astimezone(LOCAL_TZ).strftime("%m-%d %H:%M")
            lines.append(f"- [{t['id']}] {t['status']} | {t['schedule_type']}({t['schedule_value']}) | next: {next_run} | {t['prompt'][:60]}")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool("pause_task", "Pause a scheduled task", {"task_id": str})
    async def pause_task(args: dict[str, Any]) -> dict[str, Any]:
        ok = await db.update_task_status(db_path, args["task_id"], "paused")
        msg = f"Task {args['task_id']} paused." if ok else f"Task {args['task_id']} not found."
        return {"content": [{"type": "text", "text": msg}]}

    @tool("resume_task", "Resume a paused task", {"task_id": str})
    async def resume_task(args: dict[str, Any]) -> dict[str, Any]:
        ok = await db.update_task_status(db_path, args["task_id"], "active")
        msg = f"Task {args['task_id']} resumed." if ok else f"Task {args['task_id']} not found."
        return {"content": [{"type": "text", "text": msg}]}

    @tool("cancel_task", "Cancel and delete a scheduled task", {"task_id": str})
    async def cancel_task(args: dict[str, Any]) -> dict[str, Any]:
        ok = await db.delete_task(db_path, args["task_id"])
        msg = f"Task {args['task_id']} cancelled." if ok else f"Task {args['task_id']} not found."
        return {"content": [{"type": "text", "text": msg}]}

    @tool(
        "generate_and_send_image",
        "Queue an AI image generation + delivery job. The tool returns immediately (within ~1 second) — the actual "
        "codex/$imagegen generation and Telegram photo delivery happen in the background and can take 1-3 minutes. "
        "USAGE: call this once with a natural-language description, then briefly tell the user 'generation started' "
        "and END THE TURN. Do NOT wait for the photo; it will arrive in a separate Telegram message when ready. "
        "caption is an optional text caption (pass empty string for no caption).",
        {"prompt": str, "caption": str},
    )
    async def generate_and_send_image(args: dict[str, Any]) -> dict[str, Any]:
        description = args["prompt"].strip()
        if not description:
            return {"content": [{"type": "text", "text": "prompt must not be empty"}], "is_error": True}
        caption = (args.get("caption") or "").strip() or None
        target = chat_id or _telegram_chat_id
        task = asyncio.create_task(
            _bg_generate_and_deliver(description, caption, bot, target, reply_to_message_id)
        )
        _BG_IMAGE_TASKS.add(task)
        task.add_done_callback(_BG_IMAGE_TASKS.discard)
        logger.info("generate_and_send_image: queued (active bg tasks=%d)", len(_BG_IMAGE_TASKS))
        if notify_state is not None:
            notify_state.setdefault("messages", []).append("[image generation queued]")
        return {"content": [{"type": "text", "text": "Image generation queued. The photo will arrive as a separate Telegram message in 1-3 minutes."}]}

    @tool(
        "send_photo",
        "Deliver a local image file (already on disk) to the user on Telegram. "
        "Use this for images that already exist — NOT for freshly AI-generated ones (use generate_and_send_image for those). "
        "image_path must be absolute. Pass empty string for caption if none.",
        {"image_path": str, "caption": str},
    )
    async def send_photo(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(args["image_path"])
        logger.info("send_photo: called path=%s caption=%r", path, args.get("caption"))
        if not path.is_absolute() or not path.exists():
            logger.error("send_photo: invalid path %s (absolute=%s, exists=%s)", path, path.is_absolute(), path.exists())
            return {"content": [{"type": "text", "text": f"image not found or path not absolute: {path}"}], "is_error": True}
        caption = (args.get("caption") or "").strip() or None
        kwargs: dict[str, Any] = {"chat_id": chat_id or _telegram_chat_id}
        if caption:
            kwargs["caption"] = caption
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        try:
            with path.open("rb") as f:
                await bot.send_photo(photo=f, **kwargs)
        except Exception as e:
            logger.exception("send_photo: bot.send_photo failed")
            return {"content": [{"type": "text", "text": f"send_photo failed: {type(e).__name__}: {e}"}], "is_error": True}
        if notify_state is not None:
            notify_state["sent"] = True
            notify_state.setdefault("messages", []).append(f"[photo: {path.name}]")
        logger.info("send_photo: delivered %s", path.name)
        return {"content": [{"type": "text", "text": f"Photo delivered ({path.name})."}]}

    return [send_message, schedule_task, list_tasks, pause_task, resume_task, cancel_task,
            generate_and_send_image, send_photo]



def clear_session_id() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


async def _make_prompt(text: str, history: str = "", timeout: int = AGENT_TIMEOUT,
                       images: list[dict] | None = None) -> AsyncGenerator[dict, None]:
    """Create async generator prompt with conversation history for context continuity."""
    time_notice = (
        f"[System: Time limit {timeout}s. "
        f"For long tasks, send intermediate findings via mcp__nanoclaw__send_message "
        f"before time runs out. Deliver results incrementally.]\n\n"
    )
    if history:
        text_content = f"{time_notice}<conversation_history>\n{history}\n</conversation_history>\n\n{text}"
    else:
        text_content = f"{time_notice}{text}"

    if images:
        content: list[dict] = list(images) + [{"type": "text", "text": text_content}]
    else:
        content = text_content

    yield {"type": "user", "message": {"role": "user", "content": content}}


async def run_agent(prompt: str, bot: Any, chat_id: int, db_path: str, history: str = "",
                    progress: dict | None = None, notify_state: dict | None = None,
                    reply_to_message_id: int | None = None,
                    images: list[dict] | None = None) -> str:
    """Returns response_text. If progress dict is passed, updates progress["last_text"] with latest assistant output."""
    if notify_state is None:
        notify_state = {"sent": False, "messages": []}
    tools = _create_tools(bot, chat_id, db_path, notify_state, reply_to_message_id=reply_to_message_id)
    mcp_server = create_sdk_mcp_server(name="nanoclaw", tools=tools)

    env = {"ANTHROPIC_API_KEY": ANTHROPIC_API_KEY}
    if ANTHROPIC_BASE_URL:
        env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL

    options = ClaudeAgentOptions(
        cwd=str(WORKSPACE_DIR),
        setting_sources=["project"],
        max_turns=_MAX_TURNS,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
            "mcp__nanoclaw__send_message",
            "mcp__nanoclaw__schedule_task",
            "mcp__nanoclaw__list_tasks",
            "mcp__nanoclaw__pause_task",
            "mcp__nanoclaw__resume_task",
            "mcp__nanoclaw__cancel_task",
            "mcp__nanoclaw__generate_and_send_image",
            "mcp__nanoclaw__send_photo",
        ],
        permission_mode="bypassPermissions",
        mcp_servers={"nanoclaw": mcp_server},
        env=env,
        max_buffer_size=10 * 1024 * 1024,  # 10MB
    )

    result_text = ""
    last_assistant_text = ""
    all_assistant_texts: list[str] = []
    turn_count = 0
    if progress is not None:
        progress["last_text"] = ""
        progress["all_texts"] = all_assistant_texts

    try:
        async for message in query(prompt=_make_prompt(prompt, history, images=images), options=options):
            if isinstance(message, AssistantMessage):
                turn_count += 1
                texts = [b.text for b in message.content if isinstance(b, TextBlock)]
                if texts:
                    last_assistant_text = "\n".join(texts)
                    all_assistant_texts.append(last_assistant_text)
                    if progress is not None:
                        progress["last_text"] = last_assistant_text
            elif isinstance(message, ResultMessage):
                if message.result:
                    result_text = message.result
    except asyncio.CancelledError:
        logger.info("Agent cancelled (result_text=%r)", result_text[:100] if result_text else "")
    except Exception:
        logger.exception("Agent error (result_text=%r)", result_text[:100] if result_text else "")
        if not result_text and not last_assistant_text:
            return "Sorry, something went wrong while processing your request."

    if turn_count >= _MAX_TURNS:
        logger.warning("Agent exhausted max_turns (%d)", _MAX_TURNS)
        if notify_state is not None:
            notify_state["max_turns_exhausted"] = True

    return result_text or last_assistant_text or "Done."


async def run_task_agent(prompt: str, bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> str:
    """Run agent for scheduled tasks — no session resume."""
    tools = _create_tools(bot, chat_id, db_path, notify_state)
    mcp_server = create_sdk_mcp_server(name="nanoclaw", tools=tools)

    env = {"ANTHROPIC_API_KEY": ANTHROPIC_API_KEY}
    if ANTHROPIC_BASE_URL:
        env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL

    options = ClaudeAgentOptions(
        cwd=str(WORKSPACE_DIR),
        setting_sources=["project"],
        max_turns=_MAX_TURNS,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
            "mcp__nanoclaw__send_message",
            "mcp__nanoclaw__schedule_task",
            "mcp__nanoclaw__list_tasks",
            "mcp__nanoclaw__pause_task",
            "mcp__nanoclaw__resume_task",
            "mcp__nanoclaw__cancel_task",
            "mcp__nanoclaw__generate_and_send_image",
            "mcp__nanoclaw__send_photo",
        ],
        permission_mode="bypassPermissions",
        mcp_servers={"nanoclaw": mcp_server},
        env=env,
        max_buffer_size=10 * 1024 * 1024,  # 10MB
    )

    result_text = ""
    last_assistant_text = ""
    try:
        async for message in query(prompt=_make_prompt(prompt), options=options):
            if isinstance(message, AssistantMessage):
                texts = [b.text for b in message.content if isinstance(b, TextBlock)]
                if texts:
                    last_assistant_text = "\n".join(texts)
            elif isinstance(message, ResultMessage):
                if message.result:
                    result_text = message.result
    except asyncio.CancelledError:
        logger.info("Task agent cancelled (result_text=%r)", result_text[:100] if result_text else "")
    except Exception:
        if not result_text and not last_assistant_text:
            logger.exception("Task agent error")
            return "Task execution failed."
        logger.debug("Ignoring query cleanup error", exc_info=True)

    return result_text or last_assistant_text or "Task completed."
