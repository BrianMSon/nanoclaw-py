"""WebSocket server for browser/terminal access to the agent."""

import json
import logging
import sys
from pathlib import Path

from nanoclaw import db
from nanoclaw.agent import run_agent, clear_session_id
from nanoclaw.config import DB_PATH, WS_TOKEN
from nanoclaw.conversations import archive_exchange, get_recent_history

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    _STATIC_DIR = Path(sys._MEIPASS) / "nanoclaw" / "static"
else:
    _STATIC_DIR = Path(__file__).parent / "static"


class WsSink:
    """Duck-typed bot adapter that sends messages over WebSocket."""

    def __init__(self, ws):
        self._ws = ws

    async def send_message(self, *, chat_id: int, text: str, **kwargs) -> None:
        if not self._ws.closed:
            await self._ws.send_json({"type": "push", "text": text})


async def _handle_index(request):
    from aiohttp import web
    index = _STATIC_DIR / "index.html"
    if not index.exists():
        return web.Response(text="UI not found", status=404)
    return web.Response(text=index.read_text(encoding="utf-8"), content_type="text/html")


async def _handle_chat(ws, text: str) -> None:
    """Simplified version of bot._handle_message — no timeout retry."""
    sink = WsSink(ws)
    history = get_recent_history(max_exchanges=10)
    notify_state: dict = {"sent": False, "messages": []}

    await ws.send_json({"type": "typing", "active": True})
    try:
        response = await run_agent(
            text, sink, 0, str(DB_PATH),
            history=history, notify_state=notify_state,
        )
    except Exception:
        logger.exception("Agent error in WS chat")
        await ws.send_json({"type": "error", "text": "Agent error occurred."})
        return
    finally:
        if not ws.closed:
            await ws.send_json({"type": "typing", "active": False})

    await archive_exchange(text, response, chat_id=0)

    if response and not ws.closed:
        await ws.send_json({"type": "response", "text": response})


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
                if text:
                    logger.info("WS message from %s: %s", peer, text[:80])
                    await _handle_chat(ws, text)
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
