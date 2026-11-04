"""
app/csrf.py
───────────
Small helpers for server-rendered templates and SPA fetches to carry the
CSRF double-submit token (see app/middleware/csrf.py).
"""

from fastapi import Request

from app.middleware.csrf import HEADER_NAME


def get_token(request: Request, default: str = "") -> str:
    return request.cookies.get("skipline_csrf", default)


def hidden_input(token: str) -> str:
    """Render the hidden field accepted by CSRFMiddleware for classic
    form posts (alternative to the X-CSRF-Token header)."""
    return f'<input type="hidden" name="csrf_token" value="{token}">'


def fetch_headers(token: str) -> dict:
    """Headers to attach to JS fetch()/XHR calls."""
    return {HEADER_NAME: token}
