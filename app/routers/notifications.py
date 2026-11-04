"""
app/routers/notifications.py
─────────────────────────────
Notification endpoints for authenticated users.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.schemas.common import APIResponse, MessageResponse
from app.services import notification_service
from models.user import User

router = APIRouter()


@router.get("/", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def list_notifications(
    unread_only: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notifs = await notification_service.get_user_notifications(db, user.id, unread_only)
    return APIResponse(data=[
        {
            "id": n.id, "type": n.type, "channel": n.channel,
            "subject": n.subject, "message": n.message,
            "is_read": n.is_read, "sent_at": n.sent_at.isoformat(),
        }
        for n in notifs
    ])


@router.post("/mark-read", response_model=MessageResponse, dependencies=[Depends(get_current_user)])
async def mark_all_read(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await notification_service.mark_notifications_read(db, user.id)
    return MessageResponse(message="All notifications marked as read")


# ════════════════ Phase 8 — NTF-004 preferences ══════════════════════════════

@router.get("/preferences", response_model=APIResponse)
async def get_notification_preferences(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    prefs = await notification_service.get_preferences(db, user.id)
    from models.notification import ESSENTIAL_TYPES, NotificationType
    return APIResponse(data={
        "disabled_types": list(prefs.disabled_types or []) if prefs else [],
        "essential_types": sorted(ESSENTIAL_TYPES),
        "available_types": [t.value for t in NotificationType],
    })


@router.put("/preferences", response_model=APIResponse)
async def set_notification_preferences(
    disabled_types: list[str],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """NTF-004: users manage optional notifications; essential security /
    payment / service-disruption notices are rejected and can never be
    disabled."""
    try:
        prefs = await notification_service.set_preferences(db, user.id, disabled_types)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(422, str(e))
    return APIResponse(
        message="Preferences updated",
        data={"disabled_types": prefs.disabled_types or []},
    )
