import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from croniter import croniter

from nanoclaw import db
from nanoclaw.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_BASE_URL,
    LOCAL_TZ,
    STATE_FILE,
    WORKSPACE_DIR,
)

logger = logging.getLogger(__name__)


def _create_tools(bot: Any, chat_id: int, db_path: str, notify_state: dict[str, bool] | None = None) -> list:
    @tool("send_message", "Send a message to the user on Telegram", {"text": str})
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        await bot.send_message(chat_id=chat_id, text=args["text"])
        if notify_state is not None:
            notify_state["sent"] = True
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
            next_run = datetime.fromisoformat(svalue).astimezone(timezone.utc).isoformat()
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

    return [send_message, schedule_task, list_tasks, pause_task, resume_task, cancel_task]



def clear_session_id() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


async def _make_prompt(text: str, history: str = "") -> AsyncGenerator[dict, None]:
    """Create async generator prompt with conversation history for context continuity."""
    if history:
        content = f"<conversation_history>\n{history}\n</conversation_history>\n\n{text}"
    else:
        content = text
    yield {"type": "user", "message": {"role": "user", "content": content}}


async def run_agent(prompt: str, bot: Any, chat_id: int, db_path: str, history: str = "") -> str:
    """Returns response_text."""
    tools = _create_tools(bot, chat_id, db_path)
    mcp_server = create_sdk_mcp_server(name="nanoclaw", tools=tools)

    env = {"ANTHROPIC_API_KEY": ANTHROPIC_API_KEY}
    if ANTHROPIC_BASE_URL:
        env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL

    options = ClaudeAgentOptions(
        cwd=str(WORKSPACE_DIR),
        setting_sources=["project"],
        max_turns=15,
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
        ],
        permission_mode="bypassPermissions",
        mcp_servers={"nanoclaw": mcp_server},
        env=env,
    )

    result_text = ""

    try:
        async for message in query(prompt=_make_prompt(prompt, history), options=options):
            if isinstance(message, ResultMessage):
                if message.result:
                    result_text = message.result
    except Exception:
        logger.exception("Agent error (result_text=%r)", result_text[:100] if result_text else "")
        if not result_text:
            return "Sorry, something went wrong while processing your request."

    return result_text or "Done."


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
        max_turns=15,
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
        ],
        permission_mode="bypassPermissions",
        mcp_servers={"nanoclaw": mcp_server},
        env=env,
    )

    result_text = ""
    try:
        async for message in query(prompt=_make_prompt(prompt), options=options):
            if isinstance(message, ResultMessage):
                if message.result:
                    result_text = message.result
    except Exception:
        if not result_text:
            logger.exception("Task agent error")
            return "Task execution failed."
        logger.debug("Ignoring query cleanup error", exc_info=True)

    return result_text or "Task completed."
