"""
app/services/ai_provider.py
───────────────────────────
LLM transport for the AI booking assistant (Phase 9 Workstream C, MASTER §7).

DESIGN PRINCIPLES (requirement C1/C9/C9a):

- The LLM is a REASONING SURFACE ONLY. It never touches the database, never
  sees credentials it could leak further, and its output is parsed strictly.
- Authoritative business data lives in the backend tool functions inside
  ai_assistant_service.py; the provider only ships messages back and forth.
- GRACEFUL UNAVAILABILITY (C9): with no provider configured (the default)
  or any transport failure, the assistant degrades to "unavailable" instead
  of erroring, inventing data, or risking a bogus booking.
- Transport: OpenAI-compatible `/chat/completions` over httpx (already a
  declared dependency). No SDK required; any compatible provider works.

The API key is sent ONLY to settings.ai_base_url and is never logged.
"""

import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger("app.ai")


class AiProviderUnavailable(Exception):
    """Raised when the AI provider is not configured or cannot be reached."""


def is_configured() -> bool:
    """True when all four provider settings are present (C9a graceful default: empty)."""
    return bool(
        settings.ai_provider.strip()
        and settings.ai_base_url.strip()
        and settings.ai_api_key.strip()
        and settings.ai_model.strip()
    )


async def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
) -> str:
    """One chat completion. Raises AiProviderUnavailable on any failure so
    callers can degrade uniformly (never a half-parsed success)."""
    if not is_configured():
        raise AiProviderUnavailable("AI assistant is not configured")

    url = settings.ai_base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.ai_model,
        "messages": messages,
        "temperature": temperature,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.ai_request_timeout_seconds) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.ai_api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            text = body["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty completion")
            return text
    except AiProviderUnavailable:
        raise
    except Exception as exc:  # transport, timeouts, schema drift — all degrade
        # Never log the key; url/base_url are safe operational context.
        logger.warning(
            "AI provider call failed (%s: %s) — degrading assistant",
            type(exc).__name__, exc,
        )
        raise AiProviderUnavailable("AI assistant is temporarily unavailable") from exc
