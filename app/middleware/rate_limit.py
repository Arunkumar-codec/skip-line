"""
app/middleware/rate_limit.py
────────────────────────────
Sliding-window rate limiter keyed by client IP.
For production, replace the in-memory store with Redis.
"""

import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int = 200, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window = window_seconds
        self._store: dict[str, deque] = defaultdict(deque)

    def _get_client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip rate limiting for static files, health checks, and testing
        from app.config import settings
        if settings.app_env == "testing" or request.url.path.startswith(("/static", "/health", "/qr_codes", "/receipts")):
            return await call_next(request)

        ip = self._get_client_ip(request)
        now = time.time()
        window_start = now - self.window
        queue = self._store[ip]

        # Remove timestamps outside the window
        while queue and queue[0] < window_start:
            queue.popleft()

        if len(queue) >= self.max_requests:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."},
                headers={"Retry-After": str(self.window)},
            )

        queue.append(now)
        return await call_next(request)
