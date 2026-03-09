import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from croniter import croniter

from nanoclaw import db
from nanoclaw.agent import run_task_agent
from nanoclaw.config import AGENT_TIMEOUT, LOCAL_TZ, SCHEDULER_INTERVAL

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _catchup_stale_tasks(db_path: str) -> None:
    """On startup, advance next_run for cron/interval tasks stuck in the past."""
    try:
        tasks = await db.get_due_tasks(db_path)
    except Exception:
        logger.exception("Failed to query tasks for catchup")
        return

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)

    for task in tasks:
        stype = task["schedule_type"]
        svalue = task["schedule_value"]
        task_id = task["id"]

        try:
            if stype == "cron":
                next_run = croniter(svalue, now_local).get_next(datetime).astimezone(timezone.utc).isoformat()
                await db.update_task_after_run(db_path, task_id, "Catchup: skipped stale execution", next_run, "active")
                logger.info("Catchup: task %s next_run advanced to %s", task_id, next_run)
            elif stype == "interval":
                next_run = (now_utc + timedelta(milliseconds=int(svalue))).isoformat()
                await db.update_task_after_run(db_path, task_id, "Catchup: skipped stale execution", next_run, "active")
                logger.info("Catchup: task %s next_run advanced to %s", task_id, next_run)
            elif stype == "once":
                await db.update_task_after_run(db_path, task_id, "Catchup: expired once task", None, "completed")
                logger.info("Catchup: once task %s marked completed (stale)", task_id)
        except Exception:
            logger.exception("Failed to catchup task %s", task_id)


def setup_scheduler(bot, db_path: str) -> AsyncIOScheduler:
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _check_tasks,
        "interval",
        seconds=SCHEDULER_INTERVAL,
        args=[bot, db_path],
        id="check_tasks",
        replace_existing=True,
        misfire_grace_time=None,  # Never skip misfired executions
        max_instances=1,  # Prevent overlapping runs
    )
    return _scheduler


async def _check_tasks(bot, db_path: str) -> None:
    logger.debug("Scheduler tick: checking for due tasks")
    try:
        tasks = await db.get_due_tasks(db_path)
    except Exception:
        logger.exception("Failed to query due tasks")
        return

    if not tasks:
        return

    logger.info("Found %d due task(s): %s", len(tasks), [t["id"] for t in tasks])

    # Execute tasks concurrently (with timeout per task already in _execute_task)
    async def _safe_execute(task):
        try:
            await _execute_task(task, bot, db_path)
        except Exception:
            logger.exception("Failed to execute task %s", task["id"])

    await asyncio.gather(*[_safe_execute(t) for t in tasks])


async def _execute_task(task: dict, bot, db_path: str) -> None:
    task_id = task["id"]
    task_chat_id = task["chat_id"]  # Use chat_id from task, not global OWNER_ID
    prompt = task["prompt"]
    logger.info("Executing task %s for chat %s: %s", task_id, task_chat_id, prompt[:80])

    wrapped_prompt = f"You are executing a scheduled task. You MUST use the send_message tool to notify the user in Telegram. Task: {prompt}"
    notify_state = {"sent": False}

    start = time.monotonic()
    result = "No result"
    try:
        agent_task = asyncio.create_task(
            run_task_agent(wrapped_prompt, bot, task_chat_id, db_path, notify_state)
        )
        done, _ = await asyncio.wait({agent_task}, timeout=AGENT_TIMEOUT)
        if done:
            result = agent_task.result()
        else:
            agent_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(agent_task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            result = "Task timed out"
            logger.warning("Scheduled task %s timed out after %ds", task_id, AGENT_TIMEOUT)

        # Fallback to avoid silent runs when the model forgets to call send_message.
        # Skip for WebSocket-originated tasks (chat_id=0) since bot is Telegram-only here.
        if not notify_state["sent"] and task_chat_id != 0:
            await bot.send_message(chat_id=task_chat_id, text=f"⏰ 스케줄 알림: {prompt}")

        duration_ms = int((time.monotonic() - start) * 1000)
        await db.log_task_run(db_path, task_id, duration_ms, "success", result=result)
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        result = f"Error: {e}"
        try:
            await db.log_task_run(db_path, task_id, duration_ms, "error", error=str(e))
        except Exception:
            logger.exception("Failed to log task run for %s", task_id)

    # Calculate next_run (always update, even on failure, to prevent infinite re-execution)
    try:
        stype = task["schedule_type"]
        svalue = task["schedule_value"]
        now_utc = datetime.now(timezone.utc)

        if stype == "cron":
            # Cron expressions are interpreted in local time
            now_local = now_utc.astimezone(LOCAL_TZ)
            next_run = croniter(svalue, now_local).get_next(datetime).astimezone(timezone.utc).isoformat()
            await db.update_task_after_run(db_path, task_id, result, next_run, "active")
            logger.info("Task %s next_run updated to %s", task_id, next_run)
        elif stype == "interval":
            next_run = (now_utc + timedelta(milliseconds=int(svalue))).isoformat()
            await db.update_task_after_run(db_path, task_id, result, next_run, "active")
            logger.info("Task %s next_run updated to %s", task_id, next_run)
        elif stype == "once":
            await db.update_task_after_run(db_path, task_id, result, None, "completed")
        else:
            logger.warning("Unknown schedule_type %s for task %s", stype, task_id)
    except Exception:
        logger.exception("CRITICAL: Failed to update next_run for task %s — pausing to prevent infinite re-execution", task_id)
        try:
            await db.update_task_after_run(db_path, task_id, "Paused: next_run update failed", task["next_run"], "paused")
        except Exception:
            logger.exception("Failed to pause stuck task %s", task_id)
