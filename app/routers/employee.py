"""
app/routers/employee.py
──────────────────────
Employee JSON API for the daily counter shift workflow (SHIFT-001..006),
queue operations (SHIFT-003), and assisted walk-in booking (PHY-001..004).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_employee
from app.schemas.common import APIResponse
from app.schemas.shift import (
    CounterShiftCloseRequest,
    CounterShiftClosingSummary,
    CounterShiftHandoverRequest,
    CounterShiftOpenRequest,
    CounterShiftPauseRequest,
    CounterShiftRead,
    WalkinBookingRequest,
)
from app.schemas.token import TokenActionRequest, TokenRead
from app.services import audit_service, shift_service, token_service
from app.websocket.manager import ws_manager
from models.counter import Counter
from models.counter_shift import CounterShiftStatus
from models.employee import Employee
from models.token import Token, TokenStatus

router = APIRouter()


def _map_value_error(e: Exception) -> HTTPException:
    if isinstance(e, PermissionError):
        return HTTPException(status_code=403, detail=str(e))
    message = str(e)
    if "not found" in message.lower():
        return HTTPException(status_code=404, detail=message)
    if "only the requesting employee" in message.lower() or "only the shift operator" in message.lower():
        return HTTPException(status_code=403, detail=message)
    if "only an approved shift" in message.lower() or "only an active shift" in message.lower():
        return HTTPException(status_code=409, detail=message)
    if "only today" in message.lower() or "period" in message.lower():
        return HTTPException(status_code=409, detail=message)
    if "required for this operation" in message.lower() or "not authorized" in message.lower():
        return HTTPException(status_code=403, detail=message)
    return HTTPException(status_code=400, detail=message)


# ── Shift Opening & Activation ────────────────────────────────────────────────


@router.post("/shifts/open-request", response_model=APIResponse)
async def open_shift_request_api(
    body: CounterShiftOpenRequest,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    try:
        shift = await shift_service.open_shift_request(db, employee, body.model_dump())
    except ValueError as e:
        raise _map_value_error(e)
    await audit_service.log_audit_event(
        db,
        action="shift.open_request",
        actor_type="employee",
        actor_id=employee.id,
        actor_email=employee.email,
        organization_id=employee.organization_id,
        target_type="counter_shift",
        target_id=str(shift.id),
        details={
            "counter_id": shift.counter_id,
            "shift_date": shift.shift_date.isoformat(),
            "requested_services": shift.requested_services,
            "device_ready": shift.device_ready,
        },
    )
    return APIResponse(
        message="Shift request submitted", data=CounterShiftRead.model_validate(shift)
    )


@router.get("/shifts", response_model=APIResponse)
async def list_my_shifts_api(
    status: Optional[str] = Query(None),
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    shifts = await shift_service.list_employee_shifts(
        db, employee.id, employee.organization_id, status_filter=status
    )
    return APIResponse(data=[CounterShiftRead.model_validate(s) for s in shifts])


@router.get("/shifts/active", response_model=APIResponse)
async def get_my_active_shift_api(
    counter_id: Optional[int] = Query(None),
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve the operator's current active shift for today."""
    today = await shift_service._tenant_today(db, employee.organization_id)
    stmt = select(shift_service.CounterShift).where(
        and_(
            shift_service.CounterShift.organization_id == employee.organization_id,
            shift_service.CounterShift.employee_id == employee.id,
            shift_service.CounterShift.shift_date == today,
            shift_service.CounterShift.status.in_([
                CounterShiftStatus.APPROVED,
                CounterShiftStatus.ACTIVE,
                CounterShiftStatus.PAUSED,
                CounterShiftStatus.CLOSE_REQUESTED,
            ]),
        )
    )
    if counter_id:
        stmt = stmt.where(shift_service.CounterShift.counter_id == counter_id)
    shift = (await db.execute(stmt)).scalars().first()
    return APIResponse(
        data=CounterShiftRead.model_validate(shift) if shift else None
    )


@router.post("/shifts/{shift_id}/activate", response_model=APIResponse)
async def activate_shift_api(
    shift_id: int,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    try:
        shift = await shift_service.activate_shift(db, employee, shift_id)
    except ValueError as e:
        raise _map_value_error(e)
    await audit_service.log_audit_event(
        db,
        action="shift.activate",
        actor_type="employee",
        actor_id=employee.id,
        actor_email=employee.email,
        organization_id=employee.organization_id,
        target_type="counter_shift",
        target_id=str(shift.id),
        details={"status": shift.status, "counter_id": shift.counter_id},
    )
    return APIResponse(
        message="Shift activated", data=CounterShiftRead.model_validate(shift)
    )


# ── SHIFT-004: Pause, Resume, Handover ────────────────────────────────────────


@router.post("/shifts/{shift_id}/pause", response_model=APIResponse)
async def pause_shift_api(
    shift_id: int,
    body: Optional[CounterShiftPauseRequest] = None,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    reason = body.reason if body else None
    try:
        shift = await shift_service.pause_shift(db, employee, shift_id, reason=reason)
    except ValueError as e:
        raise _map_value_error(e)
    await audit_service.log_audit_event(
        db,
        action="shift.pause",
        actor_type="employee",
        actor_id=employee.id,
        actor_email=employee.email,
        organization_id=employee.organization_id,
        target_type="counter_shift",
        target_id=str(shift.id),
        details={"reason": reason},
    )
    return APIResponse(message="Shift paused", data=CounterShiftRead.model_validate(shift))


@router.post("/shifts/{shift_id}/resume", response_model=APIResponse)
async def resume_shift_api(
    shift_id: int,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    try:
        shift = await shift_service.resume_shift(db, employee, shift_id)
    except ValueError as e:
        raise _map_value_error(e)
    await audit_service.log_audit_event(
        db,
        action="shift.resume",
        actor_type="employee",
        actor_id=employee.id,
        actor_email=employee.email,
        organization_id=employee.organization_id,
        target_type="counter_shift",
        target_id=str(shift.id),
    )
    return APIResponse(message="Shift resumed", data=CounterShiftRead.model_validate(shift))


@router.post("/shifts/{shift_id}/handover", response_model=APIResponse)
async def handover_shift_api(
    shift_id: int,
    body: CounterShiftHandoverRequest,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    try:
        shift = await shift_service.handover_shift(
            db, employee, shift_id, body.target_employee_id, notes=body.notes
        )
    except ValueError as e:
        raise _map_value_error(e)
    await audit_service.log_audit_event(
        db,
        action="shift.handover",
        actor_type="employee",
        actor_id=employee.id,
        actor_email=employee.email,
        organization_id=employee.organization_id,
        target_type="counter_shift",
        target_id=str(shift.id),
        details={"target_employee_id": body.target_employee_id, "notes": body.notes},
    )
    return APIResponse(message="Shift handover recorded", data=CounterShiftRead.model_validate(shift))


# ── SHIFT-005: Shift Closing ──────────────────────────────────────────────────


@router.get("/shifts/{shift_id}/closing-summary", response_model=APIResponse)
async def get_closing_summary_api(
    shift_id: int,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    try:
        summary = await shift_service.get_shift_closing_summary(
            db, employee.organization_id, shift_id
        )
    except ValueError as e:
        raise _map_value_error(e)
    return APIResponse(data=CounterShiftClosingSummary(**summary))


@router.post("/shifts/{shift_id}/close-request", response_model=APIResponse)
async def request_close_shift_api(
    shift_id: int,
    body: CounterShiftCloseRequest,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    try:
        shift = await shift_service.request_close_shift(
            db, employee, shift_id, counted_cash=body.counted_cash, remarks=body.remarks
        )
    except ValueError as e:
        raise _map_value_error(e)
    await audit_service.log_audit_event(
        db,
        action="shift.close_request",
        actor_type="employee",
        actor_id=employee.id,
        actor_email=employee.email,
        organization_id=employee.organization_id,
        target_type="counter_shift",
        target_id=str(shift.id),
        details={
            "counted_cash": str(body.counted_cash),
            "expected_cash": str(shift.closing_cash_expected),
            "variance": str(shift.closing_cash_variance),
        },
    )
    return APIResponse(
        message="Shift closing report submitted for reconciliation",
        data=CounterShiftRead.model_validate(shift),
    )


# ── SHIFT-003: Queue Operations Guarded by Active Shift ───────────────────────


@router.get("/counters/{counter_id}/queue", response_model=APIResponse)
async def get_counter_queue_api(
    counter_id: int,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve active and waiting tokens for a counter."""
    counter = await db.get(Counter, counter_id)
    if not counter or counter.organization_id != employee.organization_id:
        raise HTTPException(status_code=404, detail="Counter not found")

    tokens_stmt = (
        select(Token)
        .where(
            and_(
                Token.counter_id == counter_id,
                Token.status.in_([TokenStatus.WAITING, TokenStatus.CALLED, TokenStatus.SERVING]),
            )
        )
        .order_by(Token.queue_position.asc(), Token.created_at.asc())
    )
    tokens = (await db.execute(tokens_stmt)).scalars().all()
    return APIResponse(data=[TokenRead.model_validate(t) for t in tokens])


@router.post("/tokens/{token_id}/action", response_model=APIResponse)
async def employee_token_action_api(
    token_id: int,
    body: TokenActionRequest,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Perform action on token (call, serve, complete, no-show, skip, cancel).

    Enforces SHIFT-003: employee MUST have an active shift for token's counter.
    """
    try:
        token = await token_service.perform_token_action(
            db,
            token_id=token_id,
            org_id=employee.organization_id,
            action=body.action,
            actor_role="employee",
            actor_id=employee.id,
            notes=body.notes,
        )
        display = await token_service.get_display_data(db, employee.organization_id)
        await ws_manager.broadcast_to_org(employee.organization_id, {"type": "display_update", "data": display})
        tracking = await token_service.get_token_tracking(db, token_id)
        await ws_manager.broadcast_to_token(token_id, {"type": "tracking_update", "data": tracking})
        return APIResponse(message=f"Token {body.action}ed", data=TokenRead.model_validate(token))
    except (ValueError, PermissionError) as e:
        raise _map_value_error(e)


@router.post("/counters/{counter_id}/call-next", response_model=APIResponse)
async def employee_call_next_api(
    counter_id: int,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    """Call the next waiting token at the counter (SHIFT-003)."""
    counter = await db.get(Counter, counter_id)
    if not counter or counter.organization_id != employee.organization_id:
        raise HTTPException(status_code=404, detail="Counter not found")

    # Enforce active shift
    try:
        await shift_service.require_active_shift(
            db, employee.organization_id, counter_id, employee.id
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    next_token_stmt = (
        select(Token)
        .where(
            and_(
                Token.counter_id == counter_id,
                Token.status == TokenStatus.WAITING,
            )
        )
        .order_by(Token.queue_position.asc(), Token.created_at.asc())
        .limit(1)
    )
    token = (await db.execute(next_token_stmt)).scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail="No waiting tokens for this counter")

    token = await token_service.perform_token_action(
        db,
        token_id=token.id,
        org_id=employee.organization_id,
        action="call",
        actor_role="employee",
        actor_id=employee.id,
    )
    display = await token_service.get_display_data(db, employee.organization_id)
    await ws_manager.broadcast_to_org(employee.organization_id, {"type": "display_update", "data": display})
    tracking = await token_service.get_token_tracking(db, token.id)
    await ws_manager.broadcast_to_token(token.id, {"type": "tracking_update", "data": tracking})

    return APIResponse(message="Next token called", data=TokenRead.model_validate(token))


# ── PHY-001..004: Assisted Walk-in Booking ────────────────────────────────────


@router.get("/customers/search", response_model=APIResponse)
async def search_walkin_customers_api(
    phone: str = Query(..., min_length=3),
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    results = await shift_service.search_walkin_customers(
        db, employee.organization_id, phone=phone
    )
    return APIResponse(data=results)


@router.post("/tokens/book-walkin", response_model=APIResponse)
async def book_walkin_token_api(
    body: WalkinBookingRequest,
    employee: Employee = Depends(get_current_employee),
    db: AsyncSession = Depends(get_db),
):
    try:
        token, payment = await shift_service.book_walkin_token(
            db, employee, body.model_dump()
        )
    except (ValueError, PermissionError) as e:
        raise _map_value_error(e)

    await audit_service.log_audit_event(
        db,
        action="token.book_walkin",
        actor_type="employee",
        actor_id=employee.id,
        actor_email=employee.email,
        organization_id=employee.organization_id,
        target_type="token",
        target_id=str(token.id),
        details={
            "token_number": token.token_number,
            "counter_id": token.counter_id,
            "payment_method": payment.payment_method,
            "payment_status": payment.status,
            "amount": str(payment.amount),
        },
    )

    display = await token_service.get_display_data(db, employee.organization_id)
    await ws_manager.broadcast_to_org(employee.organization_id, {"type": "display_update", "data": display})

    return APIResponse(
        message="Walk-in token booked successfully",
        data={
            "token": TokenRead.model_validate(token),
            "payment_id": payment.id,
            "payment_status": payment.status,
            "payment_method": payment.payment_method,
            "amount": str(payment.amount),
        },
    )
