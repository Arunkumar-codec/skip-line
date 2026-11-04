"""
tests/unit/test_phase8_security.py
──────────────────────────────────
Phase 8 boundary controls (SEC-004):

- CSRF double-submit cookie: 403 on missing/mismatched token for
  cookie-authenticated unsafe requests; Bearer/device/webhook/WS
  authorization unaffected; form-field tokens supported with body
  replay (downstream handlers can still read the form);
- security boundary headers: CSP, XFO, nosniff, referrer, permissions,
  HSTS only on https;
- rate limiter: three layers (global IP, auth-path IP, per-principal
  account), in-memory behavior, and the Redis degradation policy —
  a Redis outage degrades to the per-worker window with protection
  still ON (never fails open).
"""

import base64
import json
from typing import Callable

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest
from starlette.responses import PlainTextResponse

from app.config import settings
from app.middleware.csrf import (
    COOKIE_NAME,
    CSRFMiddleware,
    HEADER_NAME,
    is_exempt,
)
from app.middleware.headers import SecurityHeadersMiddleware
from app.middleware.rate_limit import (
    MemorySlidingWindow,
    RateLimitMiddleware,
    RedisFixedWindow,
    build_store,
)


# ── CSRF helpers ──────────────────────────────────────────────────────────────

def _scope(
    method: str = "POST",
    path: str = "/admin/api/organizations/1/suspend",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    cookies: dict[str, str] | None = None,
    body: bytes = b"",
):
    headers = [(b"host", b"testserver")]
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", cookie_str.encode()))
    headers.extend(extra_headers or [])
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "http",
    }


def _cookie_request(
    *,
    csrf_cookie: str | None = "tok",
    csrf_header: str | None = None,
    form_body: bytes | None = None,
    method="POST",
):
    body = form_body or b""
    headers = [(b"host", b"testserver")]
    if form_body is not None:
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
    cookies = {"user_token": "u-session"}
    if csrf_cookie is not None:
        cookies[COOKIE_NAME] = csrf_cookie
    if csrf_header is not None:
        headers.append((HEADER_NAME.lower().encode(), csrf_header.encode()))
    scope = _scope(method=method, cookies=cookies, extra_headers=headers)
    if body:
        scope["headers"].append((b"content-length", str(len(body)).encode()))

    async def receive():
        if not body:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": body, "more_body": False}

    request = StarletteRequest(scope)  # receive attached below
    request._receive = receive  # noqa: SLF001 — direct-dispatch harness
    return request


def _middleware(enforce=True):
    return CSRFMiddleware(app=None, enforce=enforce)  # app unused in dispatch


def _call_next(recorder):
    async def call_next(request):
        recorder.append(request)
        return PlainTextResponse("OK")
    return call_next


# ── CSRF ──────────────────────────────────────────────────────────────────────

class TestCSRFMiddleware:
    async def test_missing_token_rejected(self):
        mw = _middleware()
        routed = []
        response = await mw.dispatch(
            _cookie_request(csrf_cookie="tok"), _call_next(routed)
        )
        assert response.status_code == 403
        assert response.headers[HEADER_NAME] == "required"
        assert routed == []  # handler never ran

    async def test_matching_header_passes(self):
        mw = _middleware()
        routed = []
        response = await mw.dispatch(
            _cookie_request(csrf_cookie="tok", csrf_header="tok"),
            _call_next(routed),
        )
        assert response.status_code == 200
        assert len(routed) == 1

    async def test_mismatched_token_rejected(self):
        mw = _middleware()
        routed = []
        response = await mw.dispatch(
            _cookie_request(csrf_cookie="tok", csrf_header="evil"),
            _call_next(routed),
        )
        assert response.status_code == 403
        assert routed == []

    async def test_matching_form_field_passes_and_body_replays(self):
        mw = _middleware()
        routed: list = []

        async def form_reading_next(request):
            doc = StarletteRequest(request.scope, request._receive)
            form = await doc.form()
            routed.append(dict(form))
            return PlainTextResponse("OK")

        request = _cookie_request(
            csrf_cookie="tok",
            form_body=b"csrf_token=tok&name=agent",
        )
        response = await mw.dispatch(request, form_reading_next)
        assert response.status_code == 200
        assert routed and routed[0].get("csrf_token") == "tok"
        assert routed[0].get("name") == "agent"  # downstream still sees data

    async def test_safe_method_passes_without_token(self):
        mw = _middleware()
        routed = []
        response = await mw.dispatch(
            _cookie_request(csrf_cookie=None, method="GET"),
            _call_next(routed),
        )
        assert response.status_code == 200
        # and the response issues a fresh CSRF cookie for the next round-trip
        assert "set-cookie" in response.headers
        assert COOKIE_NAME in response.headers["set-cookie"]

    async def test_bearer_requests_exempt(self):
        mw = _middleware()
        routed = []
        scope = _scope(
            extra_headers=[(b"authorization", b"Bearer abc.def.ghi")],
            cookies={"user_token": "u"},
        )
        request = StarletteRequest(scope)
        request._receive = _noop_receive()
        response = await mw.dispatch(request, _call_next(routed))
        assert response.status_code == 200
        assert len(routed) == 1

    async def test_public_kiosk_and_webhook_paths_exempt(self):
        mw = _middleware()
        for path in ("/public/kiosk/book", "/payments/webhook", "/ws/queue"):
            routed = []
            scope = _scope(path=path, cookies={"user_token": "u"})
            request = StarletteRequest(scope)
            request._receive = _noop_receive()
            response = await mw.dispatch(request, _call_next(routed))
            assert response.status_code == 200, path
            assert len(routed) == 1, path

    async def test_enforce_flag_resolution(self, monkeypatch):
        # tests run in the testing env where CSRF is opt-in
        monkeypatch.setattr(settings, "csrf_enforce", False)
        assert CSRFMiddleware(app=None).enforce is False
        monkeypatch.setattr(settings, "csrf_enforce", True)
        assert CSRFMiddleware(app=None).enforce is True

    async def test_exempt_detection(self):
        assert is_exempt("/payments/webhook")
        assert is_exempt("/public/kiosk/book")
        assert is_exempt("/ws")
        assert not is_exempt("/admin/api/organizations/1/suspend")


def _noop_receive():
    async def receive():
        return {"type": "http.disconnect"}
    return receive


# ── Security headers ──────────────────────────────────────────────────────────

class TestSecurityHeaders:
    async def test_boundary_headers_on_every_response(self):
        mw = SecurityHeadersMiddleware(app=None)

        async def call_next(request):
            return PlainTextResponse("hi")

        response = await mw.dispatch(
            _request_for(scheme="http"), call_next
        )
        csp = response.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in response.headers["Permissions-Policy"]
        # HSTS only makes sense over https
        assert "Strict-Transport-Security" not in response.headers

    async def test_hsts_on_https(self):
        mw = SecurityHeadersMiddleware(app=None)

        async def call_next(request):
            return PlainTextResponse("hi")

        response = await mw.dispatch(_request_for(scheme="https"), call_next)
        assert response.headers["Strict-Transport-Security"].startswith("max-age=")


def _request_for(*, scheme: str) -> StarletteRequest:
    scope = _scope(method="GET", cookies={})
    scope["scheme"] = scheme
    request = StarletteRequest(scope)
    request._receive = _noop_receive()
    return request


# ── Rate limiter ──────────────────────────────────────────────────────────────

class TestMemoryWindow:
    def test_window_overflow_refuses(self):
        window = MemorySlidingWindow()
        for _ in range(3):
            allowed, _ = window.hit("k", 60, 3)
            assert allowed
        allowed, _ = window.hit("k", 60, 3)
        assert allowed is False

    def test_distinct_keys_independent(self):
        window = MemorySlidingWindow()
        for _ in range(3):
            window.hit("a", 60, 3)
        allowed, _ = window.hit("b", 60, 3)
        assert allowed is True


class TestRedisStorePolicy:
    def test_unconfigured_redis_builds_memory_store(self, monkeypatch):
        monkeypatch.setattr(settings, "redis_url", "")
        assert isinstance(build_store(), MemorySlidingWindow)

    def test_redis_error_degrades_to_memory_and_never_fails_open(self, monkeypatch):
        store = RedisFixedWindow()
        monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:6399/0")
        store._disabled = False

        class _BoomClient:
            def incr(self, key):
                raise ConnectionError("redis down")

            def expire(self, key, ttl):
                raise ConnectionError("redis down")

        class _FailingFallback:
            def __init__(self):
                self.hits = 0

            def hit(self, key, window, limit):
                self.hits += 1
                return True, window

        fallback = _FailingFallback()
        store._client = _BoomClient()
        import app.middleware.rate_limit as rl
        original_fallback = rl._FALLBACK
        rl._FALLBACK = fallback
        try:
            allowed, _ = store.hit("acct:1", 60, 10)
            assert allowed is True          # refused NOTHING: memory took over
            assert fallback.hits == 1       # the fallback actually served
            assert store._disabled is True  # stop hammering a down Redis
        finally:
            rl._FALLBACK = original_fallback


class _PrincipalTokenApp:
    """Builds a FastAPI app with the rate limiter at reduced limits."""

    @staticmethod
    def build(max_requests=3, auth_max=2, account_max=2, window=60):
        app = FastAPI()
        app.add_middleware(
            RateLimitMiddleware,
            max_requests=max_requests,
            window_seconds=window,
            auth_max_requests=auth_max,
            account_max_requests=account_max,
        )

        @app.get("/ok")
        async def ok():
            return {"ok": True}

        @app.post("/auth/login")
        async def login():
            return {"ok": True}

        return app


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": sub}).encode()
    ).rstrip(b"=").decode()
    return f"hdr.{payload}.sig"


@pytest.fixture
def rate_limit_env(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "redis_url", "")
    return monkeypatch


class TestRateLimitMiddlewareLayers:
    def test_global_ip_window(self, rate_limit_env):
        client = TestClient(_PrincipalTokenApp.build(max_requests=3))
        for _ in range(3):
            assert client.get("/ok").status_code == 200
        assert client.get("/ok").status_code == 429

    def test_auth_path_stricter_bucket(self, rate_limit_env):
        client = TestClient(_PrincipalTokenApp.build(max_requests=10, auth_max=2))
        assert client.post("/auth/login").status_code == 200
        assert client.post("/auth/login").status_code == 200
        # the auth bucket rejects before the looser global window would
        assert client.post("/auth/login").status_code == 429
        # and normal paths remain usable (global window still open)
        assert client.get("/ok").status_code == 200

    def test_account_bucket_binds_identity_not_ip(self, rate_limit_env):
        client = TestClient(_PrincipalTokenApp.build(max_requests=50, account_max=2))
        assert client.get("/ok").status_code == 200
        assert client.get("/ok").status_code == 200
        response = None
        for _ in range(6):
            response = client.get(
                "/ok", headers={"Authorization": f"Bearer {_fake_jwt('42')}"}
            )
            if response.status_code == 429:
                break
        assert response is not None
        assert response.status_code == 429  # per-principal bucket exhausted
        assert "Retry-After" in response.headers

    def test_testing_env_short_circuits(self, monkeypatch):
        monkeypatch.setattr(settings, "app_env", "testing")
        monkeypatch.setattr(settings, "redis_url", "")
        client = TestClient(_PrincipalTokenApp.build(max_requests=1))
        for _ in range(3):
            assert client.get("/ok").status_code == 200
