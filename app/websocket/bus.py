"""
app/websocket/bus.py
────────────────────
Event distribution backend for WebSocket broadcasts (Phase 6 — Redis Pub/Sub
for multi-worker deployments).

The `WebSocketManager` publishes every broadcast exactly once to a logical
channel; each worker process receives its own copy of the message through
its bus subscription and delivers it to locally-connected sockets only.
There is no hop-count problem: subscribers never re-publish.

Backends:
  - `InMemoryBus`  — default, zero-dependency. `publish` invokes registered
    handlers synchronously, so single-worker behaviour (all tests, dev) is
    identical to the distributed path.
  - `RedisBus`     — used only when `settings.redis_url` is non-empty.
    Publishes over Redis pub/sub; a per-worker listener loop reconnects with
    exponential backoff. If publishing fails (Redis briefly down), the
    message is still delivered to local sockets so in-worker updates are
    never lost; cross-worker delivery resumes on reconnect.

Channels (pattern `skipline:ws:*`):
    skipline:ws:org:{org_id}    → display screens / operator tabs
    skipline:ws:token:{token_id} → individual live tracking
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from loguru import logger

CHANNEL_PREFIX = "skipline:ws:"

Handler = Callable[[str, bytes], Awaitable[None]]
"""async (channel, raw_message) → deliver to local sockets."""


def safe_redis_endpoint(url: str) -> str:
    """Loggable connection summary — host, port, and database number only.

    Never includes the username, password, or query string that may be
    embedded in the configured URL. Handles malformed URLs (including ports
    outside int range, which raise ValueError inside urlsplit) without ever
    echoing the raw input.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or "<unspecified-host>"
        db = parts.path.lstrip("/")
        scheme = parts.scheme or "redis"
        # schemeless input is not parsed as a URL: the "path" could carry the
        # credential text room, so never echo it without an authority
        if not parts.netloc or "@" in db:
            return f"{scheme}://<unspecified-host>"
        if parts.port is not None:
            base = f"{scheme}://{host}:{parts.port}"
        else:
            base = f"{scheme}://{host}"
        return f"{base}/{db}" if db else base
    except Exception:
        return "<invalid-redis-url>"


_CRED_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/]*@[^\s]*")


def redact_redis_credentials(text: str) -> str:
    """Strip `scheme://[user[:pass]@]host...` segments that may carry secrets."""
    return _CRED_URL_RE.sub("<redacted-url>", text)


def org_channel(org_id: int) -> str:
    return f"{CHANNEL_PREFIX}org:{org_id}"


def token_channel(token_id: int) -> str:
    return f"{CHANNEL_PREFIX}token:{token_id}"


def parse_channel(channel: str) -> Optional[tuple[str, int]]:
    """Split `skipline:ws:{kind}:{id}` back out; None for foreign channels."""
    if not channel.startswith(CHANNEL_PREFIX):
        return None
    rest = channel[len(CHANNEL_PREFIX):]
    kind, _, id_part = rest.partition(":")
    if kind not in ("org", "token") or not id_part.isdigit():
        return None
    return kind, int(id_part)


class InMemoryBus:
    """Single-process bus: publish → local handlers, in order."""

    def __init__(self):
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def unsubscribe(self, handler: Handler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    async def publish(self, channel: str, message: Any) -> None:
        raw = message if isinstance(message, bytes) else str(message)
        for handler in list(self._handlers):
            try:
                await handler(channel, raw)
            except Exception:
                logger.exception("Bus handler failed for channel {}", channel)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self._handlers.clear()


class RedisBus:
    """
    Redis-backed bus for multi-worker WebSocket fan-out.

    - publishes payloads unchanged over `skipline:ws:*` channels
    - each worker runs `start()` once: it psubscribes and forwards every
      matching message to the registered local handler
    - reconnect loop retries with exponential backoff (0.5s → 30s cap) and
      logs every failure; publishing falls back to local delivery so no
      update is silently dropped while Redis is unavailable.

    The `redis` package is imported lazily: environments that never set
    `redis_url` (tests, most dev runs) do not need it installed.
    """

    def __init__(
        self,
        redis_url: str,
        reconnect_interval: float = 0.5,
        reconnect_max_interval: float = 30.0,
    ):
        if not redis_url:
            raise ValueError("RedisBus requires a redis_url")
        self._redis_url = redis_url
        self._handlers: list[Handler] = []
        self._client: Any = None
        self._listener_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._base_interval = reconnect_interval
        self._max_interval = reconnect_max_interval

    # ── Handler wiring ───────────────────────────────────────────────────────

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def unsubscribe(self, handler: Handler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    # ── Publishing ───────────────────────────────────────────────────────────

    async def publish(self, channel: str, message: Any) -> None:
        raw = message if isinstance(message, bytes) else str(message)
        try:
            client = await self._ensure_client()
            await client.publish(channel, raw)
        except Exception as exc:
            # Graceful degradation (requirements: never silently lose updates):
            # deliver locally instead — the worker publishing the change still
            # reaches its own sockets; other workers resync on reconnect.
            logger.warning(
                "Redis publish failed for {} — falling back to local delivery: {!r}",
                channel, redact_redis_credentials(repr(exc)),
            )
            await self._dispatch_local(channel, raw)

    # ── Consume / lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        if self._listener_task is not None:
            return
        self._stopping = False
        self._listener_task = asyncio.create_task(self._listener_loop())

    async def stop(self) -> None:
        self._stopping = True
        task, self._listener_task = self._listener_task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: S110 — shutdown path
                pass
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        self._handlers.clear()

    async def _ensure_client(self):
        if self._client is None:
            import redis.asyncio as redis_async  # lazy — optional dependency

            self._client = redis_async.from_url(self._redis_url, decode_responses=False)
        return self._client

    async def _listener_loop(self) -> None:
        """psubscribe `skipline:ws:*`; reconnect forever with backoff."""
        delay = self._base_interval
        while not self._stopping:
            client = None
            pubsub = None
            try:
                client = await self._ensure_client()
                pubsub = client.pubsub()
                await pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
                # Never log the raw URL: it may embed credentials.
                logger.info(
                    "Redis bus connected ({})", safe_redis_endpoint(self._redis_url)
                )
                delay = self._base_interval  # reset backoff on success

                async for message in pubsub.listen():
                    if self._stopping:
                        break
                    # message: {type: pmessage, channel, data}
                    if message.get("type") != "pmessage":
                        continue
                    channel = message.get("channel")
                    data = message.get("data")
                    if isinstance(channel, bytes):
                        channel = channel.decode("utf-8", "replace")
                    if isinstance(data, str):
                        data = data.encode("utf-8")
                    if isinstance(data, bytes):
                        await self._dispatch_local(channel, data)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Exception reprs may echo the configured URL (redis-py raises
                # e.g. "Error connecting to redis://:password@host:6379...").
                logger.warning(
                    "Redis bus listener error: {!r}",
                    redact_redis_credentials(repr(exc)),
                )
            finally:
                # Subscription cleanup: never leak pubsub connections.
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass
                if self._client is not None and client is not None:
                    self._client = None  # force a fresh client on reconnect

            if self._stopping:
                break
            logger.warning(
                "Redis bus disconnected — reconnecting in {:.1f}s", delay
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_interval)

    async def _dispatch_local(self, channel: str, raw: bytes) -> None:
        # Only skipline:ws:* channels are consumed by this plugin-style bus.
        if parse_channel(channel) is None:
            return
        for handler in list(self._handlers):
            try:
                await handler(channel, raw)
            except Exception:
                logger.exception("Bus handler failed for channel {}", channel)


def create_bus_from_settings() -> Any:
    """Bus selected from configuration: Redis when configured, else in-memory."""
    from app.config import settings

    if settings.redis_url:
        return RedisBus(settings.redis_url)
    return InMemoryBus()
