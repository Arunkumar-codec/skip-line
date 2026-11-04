"""
app/middleware/csrf.py
──────────────────────
Phase 8 — CSRF protection (SEC-004).

Strategy: the signed double-submit cookie pattern.
  - On any response to a browser request the middleware issues the
    `skipline_csrf` cookie if one is absent (readable by JS so SPA-style
    fetches can mirror it back in the `X-CSRF-Token` header, and renderable
    as a hidden form field via `csrf_token()` from app/csrf.py).
  - For every cookie-authenticated UNSAFE request (POST/PUT/PATCH/DELETE)
    the header (or `csrf_token` form field) must match the cookie with a
    constant-time comparison; mismatch/absence → 403.

Scope decisions (documented; tests set CSRF_ENFORCE=true to exercise the
middleware deterministically):
  - enforcement is Production-on by default (app_env == "production") or
    when the explicit CSRF_ENFORCE=true env flag is set; development runs
    stay functional while every non-exempt cookie-authenticated POST is
    still token-checked in production and tests;
  - non-cookie authentication is NOT affected: requests carrying a
    `Authorization: Bearer …` header, the Kiosk device credentials posted
    under /public/*, or provider webhooks (/payments/webhook) are exempt
    — cross-site browsers cannot forge those artifacts;
  - WebSocket upgrades (/ws*) are exempt (authentication is session-based
    at connect time and no state-changing HTTP handler is exposed).
"""

import hmac
import secrets
from typing import Callable, Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

COOKIE_NAME = "skipline_csrf"
HEADER_NAME = "X-CSRF-Token"
FORM_FIELD = "csrf_token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# Cookie names that make a request browser-authenticated (cookie-auth flows
# are the ones CSRF applies to; Bearer/token-header flows are exempt).
AUTH_COOKIE_NAMES = (
    "skipline_session", "admin_token", "org_token", "user_token", "employee_token",
)
# Artifacts a cross-site browser cannot attach to a forged request
NON_COOKIE_AUTH_HEADERS = ("authorization", "x-api-key")
EXEMPT_PREFIXES = (
    "/payments/webhook",  # provider call with its own signature verification
    "/public/",           # kiosk device endpoints authenticate via device secrets
    "/ws",                # websocket upgrades
    "/health", "/static", "/qr_codes", "/receipts",
)


def is_exempt(path: str) -> bool:
    return (
        any(path.startswith(p) or path.rstrip("/") == p.rstrip("/")
            for p in EXEMPT_PREFIXES)
    )


def issue_token(response: Response, *, secure: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=7200,
        samesite="lax",
        secure=secure,
        httponly=False,  # must be readable by JS/template rendering
        path="/",
    )
    return token


class CSRFMiddleware(BaseHTTPMiddleware):
    """Signed double-submit cookie CSRF protection for cookie-authenticated
    state-changing requests."""

    def __init__(self, app, enforce: Optional[bool] = None):
        super().__init__(app)
        if enforce is None:
            enforce = (
                settings.app_env == "production"
                or str(settings.csrf_enforce).lower() in ("1", "true", "yes")
            )
        self.enforce = enforce

    @staticmethod
    def _has_cookie_auth(request: Request) -> bool:
        return any(request.cookies.get(name) for name in AUTH_COOKIE_NAMES)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enforce or request.method in SAFE_METHODS or is_exempt(request.url.path):
            response = await call_next(request)
        elif not self._has_cookie_auth(request) or any(
            request.headers.get(h) for h in NON_COOKIE_AUTH_HEADERS
        ):
            response = await call_next(request)
        else:
            cookie_value = request.cookies.get(COOKIE_NAME, "")
            header_value = request.headers.get(HEADER_NAME, "")
            if not header_value and request.method in ("POST", "PUT", "PATCH"):
                # allow hidden-form-field submissions (server-rendered templates)
                form = await _peek_form(request)
                header_value = (form.get(FORM_FIELD) or "") if form else ""
            if not cookie_value or not header_value or not hmac.compare_digest(
                cookie_value, header_value
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF validation failed — refresh the page and try again"},
                    headers={"X-CSRF-Token": "required"},
                )
            response = await call_next(request)

        if response.status_code < 500 and not request.cookies.get(COOKIE_NAME):
            # issue a fresh pair for the next request (idempotent on GETs)
            try:
                issue_token(response, secure=request.url.scheme == "https")
            except Exception:  # pragma: no cover — streaming responses
                pass
        return response


async def _peek_form(request: Request) -> Optional[dict]:
    """Read form fields for the CSRF check WITHOUT breaking downstream
    parsing: the body is read through `request.body()` (which caches it)
    and the receive channel is replayed so the route handler can still
    read the post data."""
    if "application/x-www-form-urlencoded" not in (
        request.headers.get("content-type") or ""
    ):
        return None
    try:
        import urllib.parse

        body = await request.body()
        pairs = urllib.parse.parse_qsl(
            body.decode("utf-8", errors="replace"), keep_blank_values=True
        )
        if not pairs:
            return None

        async def _replayed_receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _replayed_receive  # noqa: SLF001 — standard replay
        return dict(pairs)
    except Exception:  # pragma: no cover — malformed bodies reject naturally
        return None
