"""
tests/unit/test_ai_provider_resilience.py
─────────────────────────────────────────
Provider-transport failure-path verification for the AI booking assistant
(MASTER 15.4, C9/C9a): timeout, rate limit, malformed response,
empty completion, and unconfigured graceful failure must ALL degrade to
AiProviderUnavailable — never a 500, never a half-parsed booking, and
NEVER a leaked API key in logs.

Deterministic: all calls run against httpx.MockTransport — no network,
no paid provider, no real credentials (a synthetic, never-registered key
string proves the redaction assertion).
"""
import httpx
import pytest

from app.config import settings
from app.services.ai_provider import (
    AiProviderUnavailable,
    chat,
    is_configured,
)

SYNTHETIC_KEY = "synthetic-never-real-key-012345"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "mock")
    monkeypatch.setattr(settings, "ai_base_url", "https://mock.invalid/v1")
    monkeypatch.setattr(settings, "ai_api_key", SYNTHETIC_KEY)
    monkeypatch.setattr(settings, "ai_model", "mock-model")
    assert is_configured()


@pytest.fixture
def transport(monkeypatch):
    """Patch ai_provider.httpx.AsyncClient to use a MockTransport handler.

    NOTE: monkeypatching `ai_provider.httpx.AsyncClient` mutates the shared
    httpx module object, so the real class is captured BEFORE the patch and
    used to build the wrapped client."""
    real_client_cls = httpx.AsyncClient
    holder = {"handler": lambda request: httpx.Response(200, json={})}

    class FakeFactory:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return real_client_cls(
                transport=httpx.MockTransport(holder["handler"]))

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("app.services.ai_provider.httpx.AsyncClient", FakeFactory)
    return holder


def _capture_errors(caplog):
    import logging
    logger = logging.getLogger("app.ai")
    logger.propagate = True
    return caplog


async def test_not_configured_raises_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "")
    assert not is_configured()
    with pytest.raises(AiProviderUnavailable):
        await chat([{"role": "user", "content": "hi"}])


async def test_provider_timeout_degrades(configured, transport, caplog):
    real_client_cls = httpx.AsyncClient
    transport["factory_real"] = real_client_cls
    received = {}

    def handler(request):
        received["auth"] = request.headers.get("Authorization", "")
        raise httpx.ConnectTimeout("simulated timeout")

    transport["handler"] = handler
    with pytest.raises(AiProviderUnavailable, match="temporarily unavailable"):
        await chat([{"role": "user", "content": "hi"}])
    # Key is sent only to the configured base URL, never logged.
    assert received["auth"] == f"Bearer {SYNTHETIC_KEY}"
    assert SYNTHETIC_KEY not in caplog.text


async def test_rate_limit_429_degrades(configured, transport):
    transport["handler"] = lambda request: httpx.Response(
        429, text="Too Many Requests")

    with pytest.raises(AiProviderUnavailable):
        await chat([{"role": "user", "content": "hi"}])


async def test_malformed_json_degrades(configured, transport):
    transport["handler"] = lambda request: httpx.Response(
        200, text="<html>Unexpected HTML instead of JSON</html>")

    with pytest.raises(AiProviderUnavailable):
        await chat([{"role": "user", "content": "hi"}])


async def test_empty_completion_degrades(configured, transport):
    transport["handler"] = lambda request: httpx.Response(
        200, json={"choices": [{"message": {"content": "   "}}]})

    with pytest.raises(AiProviderUnavailable):
        await chat([{"role": "user", "content": "hi"}])


async def test_error_body_never_leaks_key_in_logs(configured, transport, caplog):
    """Even a message body echoing the key must not put it in app logs
    (the logger receives only exception type/message from the transport
    layer — assert our contract on several failure shapes)."""
    transport["handler"] = lambda request: httpx.Response(500, text=SYNTHETIC_KEY)
    with pytest.raises(AiProviderUnavailable):
        await chat([{"role": "user", "content": "hi"}])
    assert SYNTHETIC_KEY not in caplog.text
