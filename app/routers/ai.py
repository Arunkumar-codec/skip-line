"""
app/routers/ai.py
─────────────────
AI booking assistant API (Phase 9 Workstreams C & D, MASTER §7/§14).

All endpoints require a signed-in USER session (`get_current_user`) — the
assistant books on behalf of the caller and can only see that user's data.

Endpoints:
- POST /ai/session            → start a conversation (mode + optional manual
                                flow context prefill — D1).
- POST /ai/conversations/{id}/message → one user turn. `confirmed: bool =
  False` IS THE USER'S explicit booking toggle (C8: never model-injectable).
  When True and a validated draft exists, the booking executes through
  token_service.book_token (C6) with live revalidation (D5).
- POST /ai/conversations/{id}/switch-mode → manual↔AI continuity (D2);
  returns the prefill payload for the manual booking form.
- GET  /ai/conversations/{id} → conversation state (mode, status, draft,
  last booking) for UI continuity (D6).

Errors: provider problems never 500 — the reply degrades via the service
("unavailable" message). Unknown/foreign conversations → uniform 404.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.schemas.common import APIResponse
from app.services import ai_assistant_service
from models.user import User

router = APIRouter(prefix="/ai", tags=["ai-assistant"])


# ── Request/response schemas ──────────────────────────────────────────────────

class AiSessionStart(BaseModel):
    mode: str = Field(default="ai", description="ai | manual")
    manual_context: Optional[dict[str, Any]] = Field(
        default=None,
        description="D1: state from the manual booking form to carry over.",
    )


class AiMessage(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    # C8: the USER's explicit confirmation. Pure HTTP parameter — the LLM
    # cannot set, suggest or fake it; bookings are impossible without it.
    confirmed: bool = Field(
        default=False,
        description="User's explicit Confirm & Book action for the pending draft.",
    )


class AiModeSwitch(BaseModel):
    mode: str = Field(description="Target mode: 'manual' or 'ai'")


def _conversation_payload(conv) -> dict[str, Any]:
    from app.services.ai_assistant_service import _draft
    return {
        "id": conv.id,
        "mode": conv.mode,
        "status": conv.status,
        "has_pending_draft": _draft(conv) is not None,
        "draft": _draft(conv),
        "last_booking_id": conv.last_booking_id,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
    }


async def _get_conv_or_404(db: AsyncSession, user: User, conversation_id: int):
    """Owner-scoped lookup; foreign or missing conversations are a uniform
    404 (tenant isolation — existence is never revealed cross-user)."""
    try:
        return await ai_assistant_service.get_conversation(db, user, conversation_id)
    except ai_assistant_service.AiConversationNotFound:
        raise HTTPException(status_code=404, detail="Conversation not found")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/session", response_model=APIResponse)
async def start_ai_session(
    body: AiSessionStart,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start (or reset context of) an assistant conversation. With
    `manual_context` the AI flow resumes exactly where the manual form left
    off (D1) — one round trip, no re-asking known answers."""
    if body.mode not in ("ai", "manual"):
        raise HTTPException(status_code=400, detail="Invalid mode")
    from app.services import ai_provider
    conv = await ai_assistant_service.start_conversation(
        db, user, mode=body.mode, manual_context=body.manual_context,
    )
    first_reply: Optional[str] = None
    if body.mode == "ai" and ai_provider.is_configured():
        # One immediate turn so the UI greetings reflect REAL availability.
        greeting = "Hi! What would you like to book today?"
        try:
            raw = await ai_assistant_service._call_chat([
                {"role": "system", "content": ai_assistant_service.SYSTEM_PROMPT},
                {"role": "user", "content": "Introduce yourself briefly."},
            ])
            greeting = str(raw)[:1500]
        except ai_provider.AiProviderUnavailable:
            greeting = ai_assistant_service.unavailable_message()
    elif body.mode == "ai":
        greeting = ai_assistant_service.unavailable_message()
    return APIResponse(data={
        "conversation_id": conv.id,
        **_conversation_payload(conv),
        "reply": greeting,
    })


@router.post("/conversations/{conversation_id}/message", response_model=APIResponse)
async def send_ai_message(
    conversation_id: int,
    body: AiMessage,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await _get_conv_or_404(db, user, conversation_id)
    try:
        outcome = await ai_assistant_service.handle_user_message(
            db, user, conv, body.message, confirmed=body.confirmed,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return APIResponse(data={
        **outcome,
        **_conversation_payload(conv),
    })


@router.post("/conversations/{conversation_id}/book", response_model=APIResponse)
async def confirm_ai_booking(
    conversation_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The user's explicit Confirm & Book action — revalidates the pending
    draft against live data (D5) and books through the standard pipeline.
    Idempotent forever: conversation-keyed idempotency + booked-status guard
    means a second click can never create a duplicate token."""
    conv = await _get_conv_or_404(db, user, conversation_id)
    outcome = await ai_assistant_service.confirm_and_book(db, user, conv)
    return APIResponse(data={**outcome, **_conversation_payload(conv)})


@router.post("/conversations/{conversation_id}/switch-mode", response_model=APIResponse)
async def switch_ai_mode(
    conversation_id: int,
    body: AiModeSwitch,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """D2: switch manual↔AI with FULL context preservation; returns the
    prefill payload for the manual booking form when switching to manual."""
    conv = await _get_conv_or_404(db, user, conversation_id)
    try:
        payload = await ai_assistant_service.switch_mode(
            db, user, conv, body.mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return APIResponse(data={**payload, **_conversation_payload(conv)})


@router.get("/conversations/{conversation_id}", response_model=APIResponse)
async def get_ai_conversation(
    conversation_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """D6: the UI can always show the assistant what it knows — mode, status,
    pending draft and completed booking (owner-scoped; others see a 404)."""
    conv = await _get_conv_or_404(db, user, conversation_id)
    return APIResponse(data=_conversation_payload(conv))
