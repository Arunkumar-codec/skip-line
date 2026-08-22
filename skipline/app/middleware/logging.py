"""
app/middleware/logging.py
─────────────────────────
Logs every HTTP request with method, path, status code, and duration.
Uses loguru for structured JSON output in production.
"""

import time
from typing import Callable

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        # Skip logging for static assets to reduce noise
        if not request.url.path.startswith("/static"):
            logger.info(
                "{method} {path} → {status} ({duration:.1f}ms)",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration=duration_ms,
            )

        return response
