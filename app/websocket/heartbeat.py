"""
app/websocket/heartbeat.py
───────────────────────────
Background task that sends a server-side ping to all connected WebSocket
clients every 30 seconds, keeping the connections alive through NAT and
proxy timeouts.

Register in app lifespan:
    asyncio.create_task(heartbeat_task())
"""

import asyncio
import json
from typing import Optional

from loguru import logger

from app.websocket.manager import ws_manager

INTERVAL_SECONDS = 30

_heartbeat_task: Optional[asyncio.Task] = None


async def start_heartbeat() -> Optional[asyncio.Task]:
    """Start the heartbeat task if not already running."""
    global _heartbeat_task
    if _heartbeat_task is not None and not _heartbeat_task.done():
        logger.debug("WebSocket heartbeat task already running.")
        return _heartbeat_task
    _heartbeat_task = asyncio.create_task(heartbeat_task(), name="websocket_heartbeat")
    return _heartbeat_task


async def stop_heartbeat() -> None:
    """Cancel and await the heartbeat task on shutdown."""
    global _heartbeat_task
    if _heartbeat_task is not None:
        if not _heartbeat_task.done():
            _heartbeat_task.cancel()
            try:
                await _heartbeat_task
            except asyncio.CancelledError:
                pass
        _heartbeat_task = None


async def heartbeat_task() -> None:
    """Runs forever; send a keep-alive ping to every connected client."""
    logger.info("WebSocket heartbeat task started (interval={}s)", INTERVAL_SECONDS)
    try:
        while True:
            await asyncio.sleep(INTERVAL_SECONDS)
            payload = json.dumps({"type": "ping"})
            org_rooms   = list(ws_manager._org_rooms.keys())
            token_rooms = list(ws_manager._token_rooms.keys())

            for org_id in org_rooms:
                try:
                    await ws_manager.broadcast_to_org(org_id, {"type": "ping"})
                except Exception:
                    pass

            for token_id in token_rooms:
                try:
                    await ws_manager.broadcast_to_token(token_id, {"type": "ping"})
                except Exception:
                    pass
    except asyncio.CancelledError:
        logger.info("WebSocket heartbeat task stopped.")
        raise
