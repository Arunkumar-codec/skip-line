"""
app/middleware/rate_limit.py
────────────────────────────
Phase 8 — Distributed rate limiting (SEC-004).

Three layers, all sliding/fixed windows keyed explicitly:
  1. GLOBAL IP bucket      — every non-static request (previous behavior,
                             200 req / 60 s default, unchanged).
  2. AUTH IP bucket        — stricter per-IP window on credential paths
                             (login / register / password recovery:
                             `rate_limit_auth_requests`, 20 / 60 s).
  3. ACCOUNT bucket        — per authenticated principal (Bearer/cookie
                             identity), `rate_limit_account_requests`,
                             300 / 60 s; unlike the IP window this holds
                             across source addresses (defends stolen
                             credentials + large-NAT harassment).

Store selection:
  - Redis (when REDIS_URL is configured): fixed-window counters via
    INCR + EXPIRE — limits hold across all application workers and are
    shared with the WebSocket fan-out infrastructure.
  - In-process sliding window otherwise (single-worker deployments).

FAILURE POLICY (SEC-004: "Redis failures must not silently disable
protections"): if Redis errors, the limiter degrades to the per-worker
in-memory window and logs a warning — it does NOT fail open, and it does
NOT envy silently: the warning is rate-limited to once per minute but
always visible.
"""

import time
from collections import defaultdict, deque
from typing import Callable, Optional, Tuple

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

AUTH_IP_LIMIT = settings.rate_limit_auth_requests
ACCOUNT_LIMIT = settings.rate_limit_account_requests

# Credential-sensitive paths get the stricter auth bucket
_AUTH_PATH_MARKERS = ("/login", "/register", "/password", "/auth/")


class MemorySlidingWindow:
    """Per-process sliding window (single-worker deployments / tests)."""

    def __init__(self) -> None:
        self._store: dict[str, deque] = defaultdict(deque)

    def hit(self, key: str, window: float, limit: int) -> Tuple[bool, int]:
        now = time.time()
        queue = self._store[key]
        cutoff = now - window
        while queue and queue[0] < cutoff:
            queue.popleft()
        if len(queue) >= limit:
            return False, window
        queue.append(now)
        return True, window


class RedisFixedWindow:
    """Cross-worker fixed window counters (INCR + EXPIRE)."""

    def __init__(self) -> None:
        self._client = None     # lazily created; disabled after a failure
        self._disabled = False

    def _get_client(self):
        if self._disabled:
            return None
        if self._client is None:
            try:
                import redis as redis_module
                from app.websocket.bus import safe_redis_endpoint

                client = redis_module.Redis.from_url(
                    safe_redis_endpoint(settings.redis_url),
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                    decode_responses=True,
                )
                client.ping()
                self._client = client
                logger.info("Distributed rate limiter: Redis store active")
            except Exception as exc:
                logger.warning(
                    "Redis rate-limit store unavailable ({}); falling back to "
                    "the per-worker in-memory window (protection remains "
                    "active, shared limits degraded to per-worker)", exc,
                )
                self._disabled = True  # stop hammering a down Redis
        return self._client

    def hit(self, key: str, window: float, limit: int) -> Tuple[bool, int]:
        client = self._get_client()
        if client is None:
            return _FALLBACK.hit(key, window, limit)
        try:
            from app.utils.redis_keys import fixed_window_key

            counter_key, period = fixed_window_key(key, window)
            count = client.incr(counter_key)
            if count == 1:
                client.expire(counter_key, int(window) + 1)
            if count > limit:
                return False, window
            return True, window
        except Exception as exc:
            # Redis failure must NOT disable protection: degrade to memory
            logger.warning(
                "Redis rate-limit error ({}); per-worker window active", exc,
            )
            self._disabled = True
            return _FALLBACK.hit(key, window, limit)


_FALLBACK = MemorySlidingWindow()


def build_store():
    """Factory used by the middleware (and tests)."""
    _ = time
    if settings.redis_url:
        return RedisFixedWindow()
    return MemorySlidingWindow()


def _resolve_principal(request: Request) -> Optional[str]:
    """Account-bucket identity: decoded JWT sub from a Bearer header or an
    HttpOnly session cookie. Anonymous requests fall through to the
    IP-only buckets."""
    import base64
    import json as _json

    candidates: list[str] = []
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        candidates.append(auth[7:].strip())
    cookie_names = ("admin_token", "org_token", "user_token", "employee_token")
    candidates.extend(request.cookies.get(c) for c in cookie_names if request.cookies.get(c))
    for token in candidates:
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
            sub = claims.get("sub")
            if sub:
                return f"acct:{sub}"
        except Exception:  # not a decodable JWT — skip that candidate
            continue
    return None


def _auth_bucket_path(path: str) -> bool:
    return any(marker in path for marker in _AUTH_PATH_MARKERS)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Layered limiter: global IP window, stricter auth-path IP window,
    and a per-principal account window. Backwards-compatible constructor
    (max_requests / window_seconds preserved)."""

    def __init__(
        self,
        app,
        max_requests: int = 200,
        window_seconds: int = 60,
        auth_max_requests: int = None,
        account_max_requests: int = None,
    ):
        super().__init__(app)
        self.max_requests = max_requests
        self.window = window_seconds
        self.auth_max_requests = auth_max_requests or AUTH_IP_LIMIT
        self.account_max_requests = account_max_requests or ACCOUNT_LIMIT
        self._store = build_store()
        self._last_warning = 0.0

    def _get_client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # the value itself is untrusted input; never logged raw
            return forwarded.split(",")[0].strip() or "unknown"
        return request.client.host if request.client else "unknown"

    def _warn_throttled(self, message: str) -> None:
        now = time.time()
        if now - self._last_warning > 60:
            self._last_warning = now
            logger.warning(message)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip rate limiting for static files, health checks, and testing
        if settings.app_env == "testing" or request.url.path.startswith((
            "/static", "/health",
        )):
            return await call_next(request)

        ip = self._get_client_ip(request)
        path = request.url.path

        verdicts: list[Tuple[str, float, str]] = []
        # 1. auth paths — stricter IP window
        if _auth_bucket_path(path):
            verdicts.append((f"ip_auth:{ip}", self.window, self.auth_max_requests))
        # 2. account window for authenticated principals
        principal = _resolve_principal(request)
        if principal:
            verdicts.append((f"{principal}:global", self.window, self.account_max_requests))
        # 3. global IP window always applies
        verdicts.append((f"ip:{ip}", self.window, self.max_requests))

        for key, window, limit in verdicts:
            allowed, remaining = self._store.hit(key, window, limit)
            if not allowed:
                self._warn_throttled(
                    f"Rate limit exceeded ({key.split(':', 1)[0]} bucket) — "
                    "request refused (limiter store active)"
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please try again later."},
                    headers={"Retry-After": str(int(window))},
                )

        return await call_next(request)
