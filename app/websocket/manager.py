"""
app/websocket/manager.py
────────────────────────
WebSocket connection manager.

Two broadcast scopes:
  - org room   (org_id)   → display screens + all operator tabs for that org
  - token room (token_id) → individual user tracking their specific token

Broadcasts are published once to a logical channel on the event bus
(app/websocket/bus.py). With the default in-process bus this delivers
locally; in multi-worker deployments Redis pub/sub fans the message out to
every worker, each of which sends only to its own connected sockets. All
send failures prune dead connections and never propagate to callers.
"""

import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from loguru import logger

from app.websocket import bus as ws_bus


class WebSocketManager:
    def __init__(self, broadcast_bus: Any = None):
        self._org_rooms: dict[int, set[WebSocket]] = defaultdict(set)
        self._token_rooms: dict[int, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._bus = broadcast_bus or ws_bus.create_bus_from_settings()
        self._handler_installed = False

    def set_broadcast_bus(self, broadcast_bus: Any) -> None:
        """Replace the bus (used by tests/startup before connections exist)."""
        self._bus = broadcast_bus
        self._handler_installed = False

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
        await self._bus.publish(ws_bus.org_channel(org_id), json.dumps(payload, default=str))

    async def broadcast_to_token(self, token_id: int, payload: Any) -> None:
        """Send a JSON message to all clients tracking a specific token."""
        await self._bus.publish(ws_bus.token_channel(token_id), json.dumps(payload, default=str))

    # ── Bus wiring ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Install the local-delivery handler and start the bus backend."""
        from app.config import settings

        if isinstance(self._bus, ws_bus.RedisBus) and not settings.redis_url:
            self._bus = ws_bus.InMemoryBus()
            self._handler_installed = False
        if not self._handler_installed:
            self._bus.subscribe(self._deliver_local)
            self._handler_installed = True
        await self._bus.start()

    async def stop(self) -> None:
        """Stop the bus backend and release handlers."""
        await self._bus.stop()
        self._handler_installed = False

    async def _deliver_local(self, channel: str, raw: Any) -> None:
        """Bus delivery path: forward one message to locally connected sockets."""
        parsed = ws_bus.parse_channel(channel)
        if parsed is None:
            return
        kind, room_id = parsed
        if kind == "org":
            await self._send_org(room_id, raw)
        else:
            await self._send_token(room_id, raw)

    async def _send_org(self, org_id: int, message: str) -> None:
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

    async def _send_token(self, token_id: int, message: str) -> None:
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
