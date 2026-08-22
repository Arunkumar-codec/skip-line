"""
app/routers/websocket.py
─────────────────────────
WebSocket endpoints.

  /ws/org/{org_id}       → display screens and operator dashboards
  /ws/token/{token_id}   → individual user tracking their token
"""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from app.websocket.manager import ws_manager

router = APIRouter()


@router.websocket("/org/{org_id}")
async def org_display_ws(org_id: int, websocket: WebSocket):
    """
    WebSocket for the public display screen and org operator dashboard.
    Clients receive `display_update` events whenever a token action occurs.
    """
    await ws_manager.connect_org(org_id, websocket)
    try:
        while True:
            # Keep connection alive; client may send ping
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.debug("Org WS disconnected org={}", org_id)
    finally:
        await ws_manager.disconnect_org(org_id, websocket)


@router.websocket("/token/{token_id}")
async def token_tracking_ws(token_id: int, websocket: WebSocket):
    """
    WebSocket for a user tracking their specific token.
    Receives `tracking_update` events when queue position changes.
    """
    await ws_manager.connect_token(token_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.debug("Token WS disconnected token={}", token_id)
    finally:
        await ws_manager.disconnect_token(token_id, websocket)
