"""
app/middleware/headers.py
─────────────────────────
Phase 8 — Security/boundary headers (SEC-004): CSP, clickjacking,
content-type sniffing, referrer leakage and permissions-surface controls.

CSP policy notes:
  - ALL vendor assets (Bootstrap CSS/JS, Bootstrap Icons, Chart.js, jsQR,
    Inter font) are served from /static/vendor — NO third-party origin is
    contacted at runtime; a template audit asserts zero external
    http(s) script/style/font references (see UIQ-005 / SEC-004 reports);
  - `script-src 'self' 'unsafe-inline'` and `style-src ... 'unsafe-inline'`:
    the pages ship Jinja-templated inline `<script>` blocks (39 files) and
    inline event handlers (100+ sites) whose portability to external files
    or delegated listeners is a tracked hardening follow-up (OPEN
    DECISIONS — strict CSP). `unsafe-inline` is an INTERIM concession,
    honestly recorded here rather than silently shipped;
  - `img-src 'self' data:` covers generated QR data URLs;
  - `frame-ancestors 'none'` + `X-Frame-Options: DENY` — clickjacking;
  - uploads/documents are never framed or opened cross-origin.
"""

from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, production_only: bool = False):
        super().__init__(app)
        self.production_only = production_only

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        headers = response.headers

        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "form-action 'self'; "
            "base-uri 'self'; "
            "object-src 'none'",
        )
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        if request.url.scheme == "https":
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
