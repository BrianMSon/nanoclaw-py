import asyncio
import atexit
import logging
import os
import subprocess
import sys
from datetime import datetime

from nanoclaw.bot import setup_bot
from nanoclaw.config import ASSISTANT_NAME, DATA_DIR, DB_PATH, LOCAL_TZ, OWNER_ID, STORE_DIR, WORKSPACE_DIR, WS_PORT, WS_TOKEN
from nanoclaw.db import init_db
from nanoclaw.memory import ensure_workspace

_LOG_FILE = DATA_DIR / "nanoclaw.log"
DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(_LOG_FILE), encoding="utf-8"),
    ],
)
# Force log timestamps to LOCAL_TZ instead of system localtime (unreliable on MSYS/Windows)
logging.Formatter.converter = lambda *args: datetime.now(LOCAL_TZ).timetuple()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_PID_FILE = DATA_DIR / "nanoclaw.pid"


def _is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _kill_existing() -> bool:
    """Kill existing instance if running. Returns True if a process was killed."""
    if not _PID_FILE.exists():
        return False
    try:
        old_pid = int(_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        return False
    if old_pid == os.getpid():
        return False
    if not _is_process_alive(old_pid):
        _PID_FILE.unlink(missing_ok=True)
        return False
    logger.info("Killing existing instance (PID %d)...", old_pid)
    try:
        if sys.platform == "win32":
            subprocess.call(["taskkill", "/F", "/T", "/PID", str(old_pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            os.kill(old_pid, signal.SIGTERM)
    except OSError as e:
        logger.warning("Failed to kill PID %d: %s", old_pid, e)
        return False
    import time
    for _ in range(10):
        if not _is_process_alive(old_pid):
            break
        time.sleep(0.5)
    _PID_FILE.unlink(missing_ok=True)
    return True


def _acquire_lock() -> None:
    """Write PID file and register cleanup."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: _PID_FILE.unlink(missing_ok=True))


async def _prepare_runtime() -> None:
    for d in (WORKSPACE_DIR, STORE_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)

    await init_db(str(DB_PATH))
    logger.info("Database initialized at %s", DB_PATH)

    ensure_workspace()
    logger.info("Workspace ready at %s", WORKSPACE_DIR)


async def _async_main(drop_pending: bool = False) -> None:
    await _prepare_runtime()

    # Telegram bot (non-blocking start)
    app = setup_bot()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=drop_pending)

    # Store Telegram bot reference so web chat send_message routes to Telegram
    from nanoclaw.agent import set_telegram_bot
    set_telegram_bot(app.bot, OWNER_ID)

    # Scheduler (moved from post_init to ensure it runs in the main event loop)
    from nanoclaw.scheduler import setup_scheduler, _catchup_stale_tasks
    logger.info("[INIT] Starting scheduler catchup...")
    await _catchup_stale_tasks(str(DB_PATH))
    logger.info("[INIT] Catchup done, setting up scheduler")
    scheduler = setup_scheduler(app.bot, str(DB_PATH))
    scheduler.start()
    logger.info("[INIT] Scheduler started (running=%s)", scheduler.running)

    # WebSocket server (if configured)
    ws_runner = None
    if WS_TOKEN:
        from nanoclaw.ws import start_ws_server
        ws_runner = await start_ws_server(WS_PORT)

    logger.info("%s is running...", ASSISTANT_NAME)
    try:
        await asyncio.Event().wait()
    finally:
        if ws_runner:
            await ws_runner.cleanup()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    killed = _kill_existing()
    if killed:
        logger.info("Previous instance terminated, waiting for Telegram session release...")
        import time
        time.sleep(3)
    _acquire_lock()
    asyncio.run(_async_main(drop_pending=killed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
