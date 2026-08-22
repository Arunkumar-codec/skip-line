"""
app/websocket/manager.py
─────────────────────────
WebSocket connection manager.

Two broadcast scopes:
  - org room   (org_id)   → display screens + all operator tabs for that org
  - token room (token_id) → individual user tracking their specific token

Thread-safe via asyncio.Lock per room.
For multi-process deployments, replace the in-process dict with Redis pub/sub.
"""

import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from loguru import logger


class WebSocketManager:
    def __init__(self):
        # org_id → set of WebSocket connections
        self._org_rooms: dict[int, set[WebSocket]] = defaultdict(set)
        # token_id → set of WebSocket connections
        self._token_rooms: dict[int, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    # ── Connection lifecycle ──────────────────────────────────────────────────

    async def connect_org(self, org_id: int, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._org_rooms[org_id].add(ws)
        logger.debug("WS connected to org room {}", org_id)

    async def connect_token(self, token_id: int, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._token_rooms[token_id].add(ws)
        logger.debug("WS connected to token room {}", token_id)

    async def disconnect_org(self, org_id: int, ws: WebSocket) -> None:
        async with self._lock:
            self._org_rooms[org_id].discard(ws)
        logger.debug("WS disconnected from org room {}", org_id)

    async def disconnect_token(self, token_id: int, ws: WebSocket) -> None:
        async with self._lock:
            self._token_rooms[token_id].discard(ws)
        logger.debug("WS disconnected from token room {}", token_id)

    # ── Broadcasting ──────────────────────────────────────────────────────────

    async def broadcast_to_org(self, org_id: int, payload: Any) -> None:
        """Send a JSON message to all WebSocket clients in an org room."""
        message = json.dumps(payload, default=str)
        dead: set[WebSocket] = set()
        async with self._lock:
            clients = set(self._org_rooms[org_id])

        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)

        if dead:
            async with self._lock:
                self._org_rooms[org_id] -= dead

    async def broadcast_to_token(self, token_id: int, payload: Any) -> None:
        """Send a JSON message to all clients tracking a specific token."""
        message = json.dumps(payload, default=str)
        dead: set[WebSocket] = set()
        async with self._lock:
            clients = set(self._token_rooms[token_id])

        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)

        if dead:
            async with self._lock:
                self._token_rooms[token_id] -= dead

    # ── Stats ─────────────────────────────────────────────────────────────────

    @property
    def org_connection_counts(self) -> dict[int, int]:
        return {k: len(v) for k, v in self._org_rooms.items() if v}

    @property
    def token_connection_counts(self) -> dict[int, int]:
        return {k: len(v) for k, v in self._token_rooms.items() if v}


# Singleton — imported by both routers and services
ws_manager = WebSocketManager()
