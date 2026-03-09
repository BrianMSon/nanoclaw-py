"""WebSocket server for browser/terminal access to the agent."""

import asyncio
import json
import logging
import sys
from pathlib import Path

from nanoclaw import db
from nanoclaw.agent import run_agent, clear_session_id
from nanoclaw.config import DB_PATH, WS_TOKEN
from nanoclaw.conversations import archive_exchange, get_recent_history

logger = logging.getLogger(__name__)

_PROGRESS_INTERVAL = 30  # seconds — match bot.py


async def _ws_report_progress(ws, msg_id: str, stop: asyncio.Event,
                              progress: dict, notify_state: dict) -> None:
    """Periodically send progress updates over WebSocket."""
    last_reported = ""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_PROGRESS_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass
        if ws.closed:
            break
        if notify_state.get("sent"):
            continue
        current = progress.get("last_text", "")
        if current and current != last_reported:
            preview = current[:200].strip()
            if len(current) > 200:
                preview += "..."
            try:
                payload: dict = {"type": "push", "text": f"⏳ 진행중:\n{preview}"}
                if msg_id:
                    payload["msg_id"] = msg_id
                await ws.send_json(payload)
            except Exception:
                pass
            last_reported = current
        elif not current:
            try:
                payload = {"type": "push", "text": "⏳ 처리 중..."}
                if msg_id:
                    payload["msg_id"] = msg_id
                await ws.send_json(payload)
            except Exception:
                pass

if getattr(sys, "frozen", False):
    _STATIC_DIR = Path(sys._MEIPASS) / "nanoclaw" / "static"
else:
    _STATIC_DIR = Path(__file__).parent / "static"


class WsSink:
    """Duck-typed bot adapter that sends messages over WebSocket."""

    def __init__(self, ws, msg_id: str | None = None):
        self._ws = ws
        self._msg_id = msg_id

    async def send_message(self, *, chat_id: int, text: str, **kwargs) -> None:
        if not self._ws.closed:
            payload: dict = {"type": "push", "text": text}
            if self._msg_id:
                payload["msg_id"] = self._msg_id
            await self._ws.send_json(payload)


async def _handle_index(request):
    from aiohttp import web
    index = _STATIC_DIR / "index.html"
    if not index.exists():
        return web.Response(text="UI not found", status=404)
    return web.Response(text=index.read_text(encoding="utf-8"), content_type="text/html")


async def _handle_chat(ws, text: str, msg_id: str) -> None:
    """Process a chat message concurrently. Tagged with msg_id."""
    sink = WsSink(ws, msg_id)
    history = get_recent_history(max_exchanges=10)
    notify_state: dict = {"sent": False, "messages": []}
    progress: dict = {"last_text": "", "all_texts": []}
    stop_progress = asyncio.Event()

    if not ws.closed:
        await ws.send_json({"type": "typing", "active": True, "msg_id": msg_id})

    progress_task = asyncio.create_task(
        _ws_report_progress(ws, msg_id, stop_progress, progress, notify_state)
    )
    try:
        response = await run_agent(
            text, sink, 0, str(DB_PATH),
            history=history, notify_state=notify_state,
            progress=progress,
        )
    except Exception:
        logger.exception("Agent error in WS chat")
        if not ws.closed:
            await ws.send_json({"type": "error", "text": "Agent error occurred.", "msg_id": msg_id})
        return
    finally:
        stop_progress.set()
        progress_task.cancel()
        if not ws.closed:
            await ws.send_json({"type": "typing", "active": False, "msg_id": msg_id})

    logger.info("Response ready for: %s", text[:80])

    await archive_exchange(text, response, chat_id=0)

    if response and not ws.closed:
        await ws.send_json({"type": "response", "text": response, "msg_id": msg_id})


async def _handle_status(ws) -> None:
    tasks = await db.get_all_tasks(str(DB_PATH))
    data = [
        {
            "id": t["id"],
            "prompt": t["prompt"],
            "status": t["status"],
            "schedule_type": t["schedule_type"],
            "schedule_value": t["schedule_value"],
            "next_run": t.get("next_run", ""),
        }
        for t in tasks
    ]
    await ws.send_json({"type": "status", "data": data})


async def _handle_ws(request):
    import aiohttp as _aiohttp
    from aiohttp import web
    peer = request.remote
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    authenticated = False
    logger.info("WS connected: %s", peer)

    async for msg in ws:
        if msg.type == _aiohttp.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "text": "Invalid JSON"})
                continue

            msg_type = data.get("type", "")

            if not authenticated:
                if msg_type == "auth":
                    if data.get("token") == WS_TOKEN:
                        authenticated = True
                        logger.info("WS authenticated: %s", peer)
                        await ws.send_json({"type": "auth_ok"})
                    else:
                        logger.warning("WS auth failed: %s", peer)
                        await ws.send_json({"type": "auth_fail", "reason": "Invalid token"})
                else:
                    await ws.send_json({"type": "auth_fail", "reason": "Not authenticated"})
                continue

            if msg_type == "message":
                text = data.get("text", "").strip()
                msg_id = data.get("msg_id", "")
                if text:
                    logger.info("WS message from %s: %s", peer, text[:80])
                    asyncio.create_task(_handle_chat(ws, text, msg_id))
            elif msg_type == "status":
                await _handle_status(ws)
            elif msg_type == "clear":
                clear_session_id()
                await ws.send_json({"type": "response", "text": "Session cleared."})
            else:
                await ws.send_json({"type": "error", "text": f"Unknown type: {msg_type}"})
        elif msg.type in (_aiohttp.WSMsgType.ERROR, _aiohttp.WSMsgType.CLOSE):
            break

    logger.info("WS disconnected: %s", peer)
    return ws


async def start_ws_server(port: int):
    from aiohttp import web
    from nanoclaw.config import WS_BIND

    app = web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/ws", _handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WS_BIND, port)
    await site.start()
    logger.info("WebSocket server started on %s:%d", WS_BIND, port)
    return runner
