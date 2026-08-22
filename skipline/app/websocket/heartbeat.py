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

from loguru import logger

from app.websocket.manager import ws_manager

INTERVAL_SECONDS = 30


async def heartbeat_task() -> None:
    """Runs forever; send a keep-alive ping to every connected client."""
    logger.info("WebSocket heartbeat task started (interval={}s)", INTERVAL_SECONDS)
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
