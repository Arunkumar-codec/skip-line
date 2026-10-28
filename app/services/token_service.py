"""
app/services/token_service.py
──────────────────────────────
Complete token lifecycle: book → call → complete / skip / cancel.
Also handles queue position calculation and real-time broadcast triggers.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from loguru import logger
from sqlalchemy import func, select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.counter import Counter, CounterStatus
from models.queue_log import QueueLog
from models.service import Service
from models.sub_service import SubService
from models.token import Token, TokenStatus
from models.token_history import TokenHistory
from models.token_service_item import TokenServiceItem
from app.utils import qr_generator


# ── Token sequence ────────────────────────────────────────────────────────────

async def _next_sequence(db: AsyncSession, counter_id: int) -> int:
    """Return the next sequence number for a counter today."""
    from datetime import date
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.coalesce(func.max(Token.sequence), 0)).where(
            and_(Token.counter_id == counter_id, Token.created_at >= today_start)
        )
    )
    return (result.scalar() or 0) + 1


# ── Queue position ─────────────────────────────────────────────────────────────

async def _recalculate_positions(db: AsyncSession, counter_id: int) -> None:
    """Recalculate queue_position for all waiting tokens at a counter."""
    result = await db.execute(
        select(Token).where(
            and_(Token.counter_id == counter_id, Token.status == TokenStatus.WAITING)
        ).order_by(Token.sequence)
    )
    waiting = result.scalars().all()
    for pos, token in enumerate(waiting, start=1):
        token.queue_position = pos
    await db.flush()


# ── Audit helpers ─────────────────────────────────────────────────────────────

async def _record_history(
    db: AsyncSession,
    token: Token,
    old_status: Optional[str],
    new_status: str,
    role: str,
    actor_id: Optional[int],
) -> None:
    db.add(TokenHistory(
        token_id=token.id,
        old_status=old_status,
        new_status=new_status,
        changed_by_role=role,
        changed_by_id=actor_id,
    ))


async def _log_action(
    db: AsyncSession,
    org_id: int,
    counter_id: Optional[int],
    token_id: Optional[int],
    action: str,
    role: str,
    actor_id: Optional[int],
    meta: Optional[dict] = None,
) -> None:
    db.add(QueueLog(
        organization_id=org_id,
        counter_id=counter_id,
        token_id=token_id,
        action=action,
        performed_by_role=role,
        performed_by_id=actor_id,
        meta=json.dumps(meta) if meta else None,
    ))


# ── Book token ────────────────────────────────────────────────────────────────

async def book_token(
    db: AsyncSession,
    org_id: int,
    counter_id: Optional[int],
    service_id: Optional[int],
    user_id: Optional[int],
    guest_name: Optional[str] = None,
    guest_mobile: Optional[str] = None,
    sub_service_ids: Optional[list[int]] = None,
    form_answers: Optional[dict[str, Any]] = None,
) -> Token:
    """
    Create a new token and assign queue position.
    Supports:
    - Multi-service counter routing when counter_id is omitted or 0
    - KYC identity verification gating (if service requires KYC)
    - Sub-services pricing and item breakdown (TokenServiceItem)
    - Dynamic custom form immutable snapshotting (TokenFormSnapshot & TokenFormAnswer)
    """
    service = None
    if service_id:
        service = await db.get(Service, service_id)
        if not service or service.organization_id != org_id:
            raise ValueError("Service not found")

        # Identity Verification Gating
        if service.requires_identity_verification:
            if not user_id:
                raise ValueError(
                    "Identity verification is required for this service. "
                    "Guest bookings are not permitted."
                )
            from app.services import identity_service
            is_verified = await identity_service.check_user_identity_verified(db, user_id, org_id)
            if not is_verified:
                raise ValueError(
                    "Identity verification required: You must have an approved identity "
                    "verification on file before booking this service."
                )

    # Multi-service Counter Routing
    if not counter_id:
        if not service_id:
            raise ValueError("Either counter_id or service_id must be provided to book a token.")
        from app.services import catalogue_service
        eligible_counters = await catalogue_service.get_eligible_counters_for_service(
            db, service_id, org_id, branch_id=service.branch_id if service else None
        )
        if not eligible_counters:
            raise ValueError("No active counter is currently available for this service.")

        # Route to eligible counter with lowest current waiting count
        best_counter = eligible_counters[0]
        min_waiting = 999999
        for c in eligible_counters:
            waiting_cnt = (await db.execute(
                select(func.count()).select_from(Token).where(
                    and_(Token.counter_id == c.id, Token.status == TokenStatus.WAITING)
                )
            )).scalar() or 0
            if waiting_cnt < min_waiting:
                min_waiting = waiting_cnt
                best_counter = c
        counter = best_counter
        counter_id = counter.id
    else:
        counter = await db.get(Counter, counter_id)
        if not counter or counter.organization_id != org_id:
            raise ValueError("Counter not found")
        if counter.status != CounterStatus.ACTIVE:
            raise ValueError("Counter is not active")

    # Check daily limit
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = (await db.execute(
        select(func.count()).select_from(Token).where(
            and_(Token.counter_id == counter_id, Token.created_at >= today_start)
        )
    )).scalar()
    if today_count >= counter.daily_limit:
        raise ValueError("Counter daily limit reached")

    seq = await _next_sequence(db, counter_id)
    token_number = f"{counter.token_prefix}-{seq:03d}"

    # Calculate pricing rollup for token snapshot
    amount_paid = service.price if service else Decimal("0.00")
    selected_subs: list[SubService] = []
    if sub_service_ids:
        subs_stmt = select(SubService).where(
            SubService.id.in_(sub_service_ids),
            SubService.is_active.is_(True),
        )
        subs_res = await db.execute(subs_stmt)
        selected_subs = list(subs_res.scalars().all())
        for s in selected_subs:
            amount_paid += s.additional_price

    token = Token(
        organization_id=org_id,
        user_id=user_id,
        counter_id=counter_id,
        service_id=service_id,
        token_number=token_number,
        sequence=seq,
        status=TokenStatus.WAITING,
        guest_name=guest_name,
        guest_mobile=guest_mobile,
        amount_paid=amount_paid,
        booked_at=datetime.now(timezone.utc),
    )
    db.add(token)
    await db.flush()   # get token.id

    # Record TokenServiceItem breakdown
    if service_id and service:
        db.add(TokenServiceItem(
            token_id=token.id,
            service_id=service_id,
            sub_service_id=None,
            price=service.price,
            estimated_minutes=service.estimated_minutes,
            sequence_order=1,
        ))
        for idx, sub_item in enumerate(selected_subs, start=2):
            db.add(TokenServiceItem(
                token_id=token.id,
                service_id=service_id,
                sub_service_id=sub_item.id,
                price=sub_item.additional_price,
                estimated_minutes=sub_item.additional_minutes,
                sequence_order=idx,
            ))

    # Form Snapshotting
    if service_id:
        from app.services import form_service
        active_form = await form_service.get_active_form_for_service(db, service_id)
        if active_form:
            await form_service.create_token_form_snapshot(
                db, token.id, active_form, form_answers or {}
            )

    # Generate QR code
    qr_path = await qr_generator.generate_token_qr(
        token_id=token.id,
        org_id=org_id,
        counter_id=counter_id,
        user_id=user_id,
    )
    token.qr_code_path = qr_path

    await _recalculate_positions(db, counter_id)
    await _record_history(db, token, None, TokenStatus.WAITING, "system", None)
    await _log_action(db, org_id, counter_id, token.id, "token_created", "system", user_id)

    await db.commit()
    await db.refresh(token)
    logger.info("Token {} created for org {}", token.token_number, org_id)
    return token


# ── Token actions (called by organization operators) ──────────────────────────

_VALID_TRANSITIONS: dict[str, list[str]] = {
    "call":     [TokenStatus.WAITING, TokenStatus.ON_HOLD],
    "recall":   [TokenStatus.CALLED],
    "hold":     [TokenStatus.CALLED, TokenStatus.IN_PROGRESS],
    "progress": [TokenStatus.CALLED],
    "complete": [TokenStatus.CALLED, TokenStatus.IN_PROGRESS],
    "skip":     [TokenStatus.WAITING, TokenStatus.CALLED],
    "cancel":   [TokenStatus.WAITING, TokenStatus.CALLED, TokenStatus.ON_HOLD],
}

_ACTION_STATUS_MAP: dict[str, str] = {
    "call":     TokenStatus.CALLED,
    "recall":   TokenStatus.CALLED,
    "hold":     TokenStatus.ON_HOLD,
    "progress": TokenStatus.IN_PROGRESS,
    "complete": TokenStatus.COMPLETED,
    "skip":     TokenStatus.SKIPPED,
    "cancel":   TokenStatus.CANCELLED,
}


async def perform_token_action(
    db: AsyncSession,
    token_id: int,
    org_id: int,
    action: str,
    actor_role: str = "organization",
    actor_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> Token:
    token = await db.get(Token, token_id)
    if not token or token.organization_id != org_id:
        raise ValueError("Token not found")

    if action not in _VALID_TRANSITIONS:
        raise ValueError(f"Unknown action: {action}")

    if token.status not in _VALID_TRANSITIONS[action]:
        raise ValueError(
            f"Cannot {action} a token with status '{token.status}'. "
            f"Expected one of: {_VALID_TRANSITIONS[action]}"
        )

    old_status = token.status
    new_status = _ACTION_STATUS_MAP[action]
    token.status = new_status
    if notes:
        token.notes = notes

    now = datetime.now(timezone.utc)
    if action == "call":
        token.called_at = now
    elif action in ("complete", "skip"):
        token.completed_at = now

    await _recalculate_positions(db, token.counter_id)
    await _record_history(db, token, old_status, new_status, actor_role, actor_id)
    await _log_action(
        db, org_id, token.counter_id, token.id,
        f"token_{action}d", actor_role, actor_id,
        meta={"old": old_status, "new": new_status},
    )

    await db.commit()
    await db.refresh(token)
    return token


# ── Live tracking ─────────────────────────────────────────────────────────────

async def get_token_tracking(db: AsyncSession, token_id: int) -> dict:
    """Compute live tracking data for a token."""
    token = await db.get(Token, token_id)
    if not token:
        raise ValueError("Token not found")

    counter = await db.get(Counter, token.counter_id)
    service = await db.get(Service, token.service_id) if token.service_id else None
    est_minutes = service.estimated_minutes if service else 10

    # People ahead = tokens waiting with lower sequence at same counter
    people_ahead = (await db.execute(
        select(func.count()).select_from(Token).where(
            and_(
                Token.counter_id == token.counter_id,
                Token.status == TokenStatus.WAITING,
                Token.sequence < token.sequence,
            )
        )
    )).scalar()

    # Current token being served
    current_result = await db.execute(
        select(Token.token_number).where(
            and_(
                Token.counter_id == token.counter_id,
                Token.status.in_([TokenStatus.CALLED, TokenStatus.IN_PROGRESS]),
            )
        ).order_by(Token.called_at.desc()).limit(1)
    )
    current_token = current_result.scalar_one_or_none()

    return {
        "token_id": token.id,
        "token_number": token.token_number,
        "status": token.status,
        "queue_position": token.queue_position,
        "people_ahead": int(people_ahead),
        "estimated_wait_minutes": int(people_ahead) * est_minutes,
        "current_token_at_counter": current_token,
        "counter_name": counter.counter_name if counter else "",
    }


# ── Public display data ────────────────────────────────────────────────────────

async def get_display_data(db: AsyncSession, org_id: int) -> dict:
    """Data for the public display screen."""
    counters_result = await db.execute(
        select(Counter).where(
            and_(Counter.organization_id == org_id, Counter.status == CounterStatus.ACTIVE)
        ).order_by(Counter.counter_number)
    )
    counters = counters_result.scalars().all()

    counter_data = []
    for c in counters:
        current = (await db.execute(
            select(Token.token_number).where(
                and_(
                    Token.counter_id == c.id,
                    Token.status.in_([TokenStatus.CALLED, TokenStatus.IN_PROGRESS]),
                )
            ).order_by(Token.called_at.desc()).limit(1)
        )).scalar_one_or_none()

        upcoming_result = await db.execute(
            select(Token.token_number).where(
                and_(Token.counter_id == c.id, Token.status == TokenStatus.WAITING)
            ).order_by(Token.sequence).limit(5)
        )
        upcoming = upcoming_result.scalars().all()

        counter_data.append({
            "counter_id": c.id,
            "counter_name": c.counter_name,
            "counter_number": c.counter_number,
            "status": c.status,
            "current_token": current,
            "upcoming_tokens": list(upcoming),
        })

    # Announcement
    from sqlalchemy import and_ as _and
    from models.setting import Setting
    ann_result = await db.execute(
        select(Setting.value).where(
            _and(Setting.organization_id == org_id, Setting.key == "announcement_text")
        )
    )
    announcement = ann_result.scalar_one_or_none() or ""

    return {"counters": counter_data, "announcement": announcement}
