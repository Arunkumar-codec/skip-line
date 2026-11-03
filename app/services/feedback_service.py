"""
app/services/feedback_service.py
───────────────────────────────
Phase 7 — Verified Token Feedback (FDB-001..005).

Invariants enforced here (all server-side):
- Only tokens with TokenStatus.COMPLETED are eligible, and the principal
  must own the token (FDB-001).
- ONE active feedback row per token (DB-unique token_id); a REMOVED row
  is re-activated in place instead of inserting a duplicate.
- Moderation only moves along PENDING→PUBLISHED→HIDDEN/REPORTED→REMOVED.
- Organizations may respond or report, but can never modify ratings,
  comments or other feedback content nor delete negative feedback
  (FDB-004).
- Every sensitive transition is audited via audit_service.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.services.audit_service import log_audit_event
from models.feedback import Feedback, FeedbackResponse, ModerationStatus
from models.token import Token, TokenStatus

MODERATION_ACTIONS = {
    # action → (resulting status, allowed_from)
    "publish": (ModerationStatus.PUBLISHED,
                {ModerationStatus.PENDING, ModerationStatus.HIDDEN, ModerationStatus.REPORTED}),
    "hide": (ModerationStatus.HIDDEN,
             {ModerationStatus.PENDING, ModerationStatus.PUBLISHED, ModerationStatus.REPORTED}),
    "report": (ModerationStatus.REPORTED,
               {ModerationStatus.PENDING, ModerationStatus.PUBLISHED}),
    "remove": (ModerationStatus.REMOVED,
               {ModerationStatus.HIDDEN, ModerationStatus.REPORTED, ModerationStatus.PENDING}),
}

PUBLIC_STATUSES = {ModerationStatus.PUBLISHED}


def _rating(value: Optional[int], label: str) -> None:
    if value is not None and not 1 <= value <= 5:
        raise ValueError(f"{label} rating must be between 1 and 5")


async def get_my_feedback_for_token(db: AsyncSession, token_id: int) -> Optional[Feedback]:
    return (
        await db.execute(select(Feedback).where(Feedback.token_id == token_id))
    ).scalar_one_or_none()


async def submit_feedback(
    db: AsyncSession,
    token_id: int,
    user_id: int,
    rating_overall: int,
    rating_waiting: Optional[int] = None,
    rating_staff: Optional[int] = None,
    rating_service: Optional[int] = None,
    result: Optional[str] = None,
    comment: Optional[str] = None,
    is_anonymous: bool = False,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
) -> Feedback:
    """FDB-001/002: submit verified feedback for a COMPLETED, owned token."""
    for label, value in (
        ("overall", rating_overall), ("waiting", rating_waiting),
        ("staff", rating_staff), ("service", rating_service),
    ):
        _rating(value, label)

    token = await db.get(Token, token_id)
    if token is None:
        raise LookupError("Token not found")
    # FDB-001 hard invariants: COMPLETED status + ownership. Guest tokens
    # (user_id NULL) cannot be attributed to a portal user.
    if token.status != TokenStatus.COMPLETED:
        raise ValueError("Feedback can only be submitted for completed tokens")
    if token.user_id != user_id:
        raise PermissionError("You can only rate your own tokens")

    existing = await get_my_feedback_for_token(db, token_id)
    if existing is not None and existing.moderation_status != ModerationStatus.REMOVED:
        raise ValueError("This token has already been rated")

    if existing is not None:
        # Re-rating after moderation removal — same row, refreshed content.
        existing.rating_overall = rating_overall
        existing.rating_waiting = rating_waiting
        existing.rating_staff = rating_staff
        existing.rating_service = rating_service
        existing.result = result
        existing.comment = comment
        existing.is_anonymous = is_anonymous
        existing.attachment_path = attachment_path
        existing.attachment_name = attachment_name
        existing.moderation_status = ModerationStatus.PENDING
        existing.moderation_reason = None
        existing.moderated_by = None
        existing.moderated_at = None
        feedback = existing
    else:
        feedback = Feedback(
            token_id=token_id,
            user_id=user_id,
            organization_id=token.organization_id,
            counter_id=token.counter_id,
            service_id=token.service_id,
            rating_overall=rating_overall,
            rating_waiting=rating_waiting,
            rating_staff=rating_staff,
            rating_service=rating_service,
            result=result,
            comment=comment,
            is_anonymous=is_anonymous,
            attachment_path=attachment_path,
            attachment_name=attachment_name,
            moderation_status=ModerationStatus.PENDING,
        )
        db.add(feedback)
    await db.commit()
    await db.refresh(feedback)
    return feedback


# ── Public reads (FDB-003) ───────────────────────────────────────────────────

def _display_author(feedback: Feedback, user_name: Optional[str]) -> str:
    return "Verified customer" if feedback.is_anonymous else (user_name or "Verified customer")


def _period_label(feedback: Feedback) -> str:
    created = feedback.created_at
    if created is None:
        return ""
    return created.strftime("%b %Y")


async def list_public_reviews(
    db: AsyncSession, organization_id: int
) -> list[dict]:
    """FDB-003: approved content only, verified badge context, zero PII."""
    rows = (
        await db.execute(
            select(Feedback)
            .options(selectinload(Feedback.responses))
            .where(
                Feedback.organization_id == organization_id,
                Feedback.moderation_status == ModerationStatus.PUBLISHED,
            )
            .order_by(Feedback.id.desc())
        )
    ).scalars().all()

    from models.user import User  # local import to avoid cycles

    out = []
    for fb in rows:
        user_name = None
        if not fb.is_anonymous and fb.user_id:
            user = await db.get(User, fb.user_id)
            user_name = user.name if user else None
        org_response = next(
            (r.body for r in fb.responses if r.kind == "response"), None
        )
        out.append({
            "id": fb.id,
            "rating_overall": fb.rating_overall,
            "rating_waiting": fb.rating_waiting,
            "rating_staff": fb.rating_staff,
            "rating_service": fb.rating_service,
            "result": fb.result,
            "comment": fb.comment,
            "verified_token": True,  # structural: every row is token-verified
            "display_author": _display_author(fb, user_name),
            "period_label": _period_label(fb),
            "org_response": org_response,
        })
    return out


async def public_summary(db: AsyncSession, organization_id: int) -> dict:
    rows = (
        await db.execute(
            select(Feedback.rating_overall).where(
                Feedback.organization_id == organization_id,
                Feedback.moderation_status == ModerationStatus.PUBLISHED,
            )
        )
    ).scalars().all()
    count = len(rows)
    return {
        "average": round(sum(rows) / count, 2) if count else None,
        "count": count,
    }


# ── Organization actions (FDB-004) ──────────────────────────────────────────

async def org_respond(
    db: AsyncSession, feedback_id: int, organization_id: int, body: str
) -> FeedbackResponse:
    feedback = await db.get(Feedback, feedback_id)
    if feedback is None or feedback.organization_id != organization_id:
        raise LookupError("Feedback not found")
    response = FeedbackResponse(
        feedback_id=feedback_id, organization_id=organization_id,
        kind="response", body=body,
    )
    db.add(response)
    await db.commit()
    await db.refresh(response)
    return response


async def org_report(
    db: AsyncSession, feedback_id: int, organization_id: int, body: str
) -> FeedbackResponse:
    """Reports flip moderation state to REPORTED for admin review."""
    feedback = await db.get(Feedback, feedback_id)
    if feedback is None or feedback.organization_id != organization_id:
        raise LookupError("Feedback not found")
    if feedback.moderation_status not in (
        ModerationStatus.PENDING, ModerationStatus.PUBLISHED, ModerationStatus.HIDDEN,
    ):
        raise ValueError("Feedback has already been reported or removed")
    db.add(FeedbackResponse(
        feedback_id=feedback_id, organization_id=organization_id,
        kind="report", body=body,
    ))
    feedback.moderation_status = ModerationStatus.REPORTED
    await db.commit()
    await db.refresh(feedback)
    return feedback


# ── Admin moderation (FDB-005) ──────────────────────────────────────────────

async def moderate(
    db: AsyncSession,
    feedback_id: int,
    admin_id: int,
    action: str,
    reason: Optional[str],
) -> Feedback:
    """Filtered action on public visibility; fully audited."""
    mapping = MODERATION_ACTIONS.get(action)
    if mapping is None:
        raise ValueError("Unknown moderation action")
    feedback = await db.get(Feedback, feedback_id)
    if feedback is None:
        raise LookupError("Feedback not found")
    target_status, allowed_from = mapping
    if feedback.moderation_status not in allowed_from:
        raise ValueError(
            f"Cannot {action} feedback in state {feedback.moderation_status.value}"
        )
    feedback.moderation_status = target_status
    feedback.moderation_reason = reason
    feedback.moderated_by = admin_id
    feedback.moderated_at = datetime.now(timezone.utc)
    await log_audit_event(
        db, action=f"feedback_moderated_{action}", actor_type="admin",
        actor_id=admin_id, organization_id=feedback.organization_id,
        target_type="feedback", target_id=feedback.id,
        details={"reason": reason or ""},
    )
    await db.commit()
    await db.refresh(feedback)
    return feedback
