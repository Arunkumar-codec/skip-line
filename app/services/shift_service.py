"""
app/services/shift_service.py
─────────────────────────────
Daily counter shift state machine (SHIFT-001/002).

Employee open request -> authorized head decision (approve/reject) ->
operator activation to an active desk. A shift is never ACTIVE without a
prior APPROVED decision.

Authorization model (SHIFT-002):
- The organization principal approves by default.
- An employee may decide when explicitly delegated via the
  `shifts:approve` permission (RBAC catalog; assignment through the
  existing roles:assign mechanism).
- Only the requesting employee activates their own approved shift.

All operations are strictly tenant-scoped: rows belonging to another
organization are indistinguishable from missing rows (404 upstream).
"""

from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from loguru import logger
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import financial_risk_service, ledger_service
from models.capacity_rule import BookingSource
from models.counter import Counter, CounterStatus
from models.counter_shift import (
    ACTIVE_SHIFT_STATUSES,
    CounterShift,
    CounterShiftStatus,
)
from models.employee import Employee
from models.finance import (
    CashSession,
    CashSessionStatus,
    LedgerAccount,
    LedgerDirection,
    LedgerEntryType,
)
from models.organization import Organization
from models.payment import Payment, PaymentMethod, PaymentStatus
from models.service import Service
from models.token import Token, TokenStatus
from models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _active_statuses_list() -> list[str]:
    return [s.value for s in ACTIVE_SHIFT_STATUSES]


async def _tenant_today(db: AsyncSession, organization_id: int) -> date:
    """Current calendar day in the tenant's timezone (Decision 12)."""
    from app.services.schedule_service import get_tenant_today

    org = await db.get(Organization, organization_id)
    return get_tenant_today(org, None)


async def _tenant_local_time(db: AsyncSession, organization_id: int) -> time:
    """Current wall-clock time in the tenant's timezone (Decision 12).

    Uses the same timezone resolution as schedule_service (`_zone` raises
    a 400 for an invalid IANA identifier; organization timezones are
    validated at configuration time, so this is a defensive path).
    """
    from app.services.schedule_service import _zone, get_timezone_name

    org = await db.get(Organization, organization_id)
    zone = _zone(get_timezone_name(org, None))
    return datetime.now(zone).time()


async def _org_setting(db: AsyncSession, organization_id: int, key: str) -> Optional[str]:
    from models.setting import Setting
    res = await db.execute(
        select(Setting.value).where(
            and_(Setting.organization_id == organization_id, Setting.key == key)
        )
    )
    return res.scalar_one_or_none()


async def get_shift(db: AsyncSession, organization_id: int, shift_id: int) -> CounterShift:
    shift = await db.get(CounterShift, shift_id)
    if not shift or shift.organization_id != organization_id:
        raise ValueError("Shift not found")
    return shift


# ── SHIFT-001: employee open request ─────────────────────────────────────────


async def open_shift_request(
    db: AsyncSession,
    employee: Employee,
    data: dict,
) -> CounterShift:
    """Submit (or correct-and-resubmit) a daily counter shift open request.

    The request date is the tenant's current day and is never taken from
    client input. A pending (OPEN_REQUESTED) row for the same
    (employee, counter, day) is updated in place — the correction flow
    for a rejected request (OPEN_DECISIONS "Shift workflow notes").
    """
    counter_id = data["counter_id"]
    counter = await db.get(Counter, counter_id)
    if not counter or counter.organization_id != employee.organization_id:
        raise ValueError("Counter not found")
    if counter.status != CounterStatus.ACTIVE:
        raise ValueError("Counter is not active")

    service_ids = [int(s) for s in (data.get("services") or [])]
    for service_id in service_ids:
        service = await db.get(Service, service_id)
        if not service or service.organization_id != employee.organization_id:
            raise ValueError(f"Service not found: {service_id}")

    today = await _tenant_today(db, employee.organization_id)

    pending = (
        await db.execute(
            select(CounterShift).where(
                and_(
                    CounterShift.organization_id == employee.organization_id,
                    CounterShift.counter_id == counter_id,
                    CounterShift.employee_id == employee.id,
                    CounterShift.shift_date == today,
                    CounterShift.status == CounterShiftStatus.OPEN_REQUESTED,
                )
            )
        )
    ).scalar_one_or_none()

    fields = {
        "requested_services": service_ids,
        "device_ready": bool(data.get("device_ready", False)),
        "opening_cash": data.get("opening_cash", Decimal("0.00")),
        "remarks": data.get("remarks"),
    }

    if pending:
        for key, value in fields.items():
            setattr(pending, key, value)
        shift = pending
    else:
        blocking = (
            await db.execute(
                select(CounterShift.id).where(
                    and_(
                        CounterShift.organization_id == employee.organization_id,
                        CounterShift.counter_id == counter_id,
                        CounterShift.employee_id == employee.id,
                        CounterShift.shift_date == today,
                        CounterShift.status.in_(_active_statuses_list()),
                    )
                )
            )
        ).scalar_one_or_none()
        if blocking:
            raise ValueError(
                "A shift request is already open for today; only one non-terminal "
                "shift per employee, counter and day is allowed"
            )
        shift = CounterShift(
            organization_id=employee.organization_id,
            counter_id=counter_id,
            employee_id=employee.id,
            shift_date=today,
            status=CounterShiftStatus.OPEN_REQUESTED,
            **fields,
        )
        db.add(shift)

    await db.commit()
    await db.refresh(shift)
    return shift


async def list_employee_shifts(
    db: AsyncSession,
    employee_id: int,
    organization_id: int,
    status_filter: Optional[str] = None,
    limit: int = 50,
) -> list[CounterShift]:
    query = select(CounterShift).where(
        and_(
            CounterShift.organization_id == organization_id,
            CounterShift.employee_id == employee_id,
        )
    )
    if status_filter:
        query = query.where(CounterShift.status == status_filter)
    query = query.order_by(CounterShift.created_at.desc()).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


async def list_org_shifts(
    db: AsyncSession,
    organization_id: int,
    status_filter: Optional[str] = None,
    shift_date: Optional[date] = None,
    limit: int = 200,
) -> list[CounterShift]:
    """Head-side listing of the organization's shifts (request queue)."""
    query = select(CounterShift).where(CounterShift.organization_id == organization_id)
    if status_filter:
        query = query.where(CounterShift.status == status_filter)
    if shift_date:
        query = query.where(CounterShift.shift_date == shift_date)
    query = query.order_by(CounterShift.created_at.asc()).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


# ── SHIFT-002: head decision ─────────────────────────────────────────────────


async def decide_shift_request(
    db: AsyncSession,
    organization_id: int,
    shift_id: int,
    decision: str,
    reason: str,
    decided_by_type: str,
    decided_by_id: Optional[int],
    approved_services: Optional[list[int]] = None,
    approved_period_start: Optional[time] = None,
    approved_period_end: Optional[time] = None,
    payment_permitted: Optional[bool] = None,
) -> CounterShift:
    """Approve or reject an OPEN_REQUESTED shift.

    A rejection keeps the row OPEN_REQUESTED with the head's reason so the
    operator can correct and resubmit; an approval records the approved
    services, optional daily period, payment permission and moves the row
    to APPROVED. Re-deciding an already decided row is not permitted.
    """
    if decision not in ("approve", "reject"):
        raise ValueError("Unknown decision")
    if not (reason or "").strip():
        raise ValueError("A decision reason is required")
    if (
        decision == "reject"
        and (approved_services is not None or approved_period_start or approved_period_end)
    ):
        raise ValueError(
            "Rejection records a reason only; approved scope applies to approvals"
        )

    shift = await get_shift(db, organization_id, shift_id)
    if shift.status != CounterShiftStatus.OPEN_REQUESTED:
        raise ValueError("Only a pending open request can be decided")

    if decision == "reject":
        # Stays OPEN_REQUESTED; the reason records the rejection/correction.
        shift.decided_at = _utcnow()
        shift.decided_by_type = decided_by_type
        shift.decided_by_id = decided_by_id
        shift.decided_reason = reason.strip()
    else:
        services = approved_services
        if services is None:
            services = list(shift.requested_services or [])
        for service_id in services:
            service = await db.get(Service, service_id)
            if not service or service.organization_id != organization_id:
                raise ValueError(f"Service not found: {service_id}")
        if (
            approved_period_start is not None
            and approved_period_end is not None
            and approved_period_start >= approved_period_end
        ):
            raise ValueError("Approved period must end after it starts")
        shift.status = CounterShiftStatus.APPROVED
        shift.approved_services = services
        shift.approved_period_start = approved_period_start
        shift.approved_period_end = approved_period_end
        shift.payment_permitted = bool(payment_permitted or False)
        shift.decided_at = _utcnow()
        shift.decided_by_type = decided_by_type
        shift.decided_by_id = decided_by_id
        shift.decided_reason = reason.strip()

    await db.commit()
    await db.refresh(shift)
    return shift


# ── Activation: approved shift -> active desk ────────────────────────────────


async def activate_shift(
    db: AsyncSession,
    employee: Employee,
    shift_id: int,
) -> CounterShift:
    """Clock the operator in: APPROVED -> ACTIVE (today's tenant day only)."""
    shift = await get_shift(db, employee.organization_id, shift_id)
    if shift.employee_id != employee.id:
        raise ValueError("Only the requesting employee can activate the shift")
    if shift.status != CounterShiftStatus.APPROVED:
        raise ValueError("Only an approved shift can be activated")

    today = await _tenant_today(db, employee.organization_id)
    if shift.shift_date != today:
        raise ValueError("Only today's shift can be activated")

    now_local = await _tenant_local_time(db, employee.organization_id)
    if shift.approved_period_start and now_local < shift.approved_period_start:
        raise ValueError("Approved period has not started yet")
    if shift.approved_period_end and now_local >= shift.approved_period_end:
        raise ValueError("Approved period has already ended")

    shift.status = CounterShiftStatus.ACTIVE
    await db.commit()
    await db.refresh(shift)
    return shift


# ── SHIFT-003 gate primitives (reusable counter-operation guard) ──────────────


async def get_active_shift(
    db: AsyncSession,
    organization_id: int,
    counter_id: int,
    employee_id: int,
) -> Optional[CounterShift]:
    """Return the operator's ACTIVE shift for the given counter, or None."""
    today = await _tenant_today(db, organization_id)
    result = await db.execute(
        select(CounterShift).where(
            and_(
                CounterShift.organization_id == organization_id,
                CounterShift.counter_id == counter_id,
                CounterShift.employee_id == employee_id,
                CounterShift.shift_date == today,
                CounterShift.status == CounterShiftStatus.ACTIVE,
            )
        )
    )
    return result.scalar_one_or_none()


async def require_active_shift(
    db: AsyncSession,
    organization_id: int,
    counter_id: int,
    employee_id: int,
) -> CounterShift:
    """Guard for any counter operation that needs an approved active shift.

    Raises PermissionError when the operator has no ACTIVE approved shift
    (SHIFT-003 / Release Gate 4): callers translate this into HTTP 403.
    """
    shift = await get_active_shift(db, organization_id, counter_id, employee_id)
    if shift is None:
        raise PermissionError("An approved, active shift is required for this operation")
    return shift


# ── SHIFT-004: pause, resume and handover ────────────────────────────────────


async def pause_shift(
    db: AsyncSession,
    employee: Employee,
    shift_id: int,
    reason: Optional[str] = None,
) -> CounterShift:
    """Pause an active shift (SHIFT-004). Operator takes a break or pauses counter."""
    shift = await get_shift(db, employee.organization_id, shift_id)
    if shift.employee_id != employee.id:
        raise ValueError("Only the shift operator can pause the shift")
    if shift.status != CounterShiftStatus.ACTIVE:
        raise ValueError("Only an active shift can be paused")

    shift.status = CounterShiftStatus.PAUSED
    shift.paused_at = _utcnow()
    shift.pause_reason = (reason or "").strip() or "Operator paused shift"

    # Set counter status to break
    counter = await db.get(Counter, shift.counter_id)
    if counter:
        counter.status = CounterStatus.BREAK

    await db.commit()
    await db.refresh(shift)
    return shift


async def resume_shift(
    db: AsyncSession,
    employee: Employee,
    shift_id: int,
) -> CounterShift:
    """Resume a paused shift (SHIFT-004)."""
    shift = await get_shift(db, employee.organization_id, shift_id)
    if shift.employee_id != employee.id:
        raise ValueError("Only the shift operator can resume the shift")
    if shift.status != CounterShiftStatus.PAUSED:
        raise ValueError("Only a paused shift can be resumed")

    shift.status = CounterShiftStatus.ACTIVE
    shift.resumed_at = _utcnow()

    counter = await db.get(Counter, shift.counter_id)
    if counter:
        counter.status = CounterStatus.ACTIVE

    await db.commit()
    await db.refresh(shift)
    return shift


async def handover_shift(
    db: AsyncSession,
    employee: Employee,
    shift_id: int,
    target_employee_id: int,
    notes: Optional[str] = None,
) -> CounterShift:
    """Hand over a counter to another employee (SHIFT-004)."""
    shift = await get_shift(db, employee.organization_id, shift_id)
    if shift.employee_id != employee.id:
        raise ValueError("Only the shift operator can hand over the shift")
    if shift.status not in (CounterShiftStatus.ACTIVE, CounterShiftStatus.PAUSED):
        raise ValueError("Only an active or paused shift can be handed over")

    target = await db.get(Employee, target_employee_id)
    if not target or target.organization_id != employee.organization_id:
        raise ValueError("Target employee not found in organization")
    if not target.is_active:
        raise ValueError("Target employee is inactive")

    shift.handover_to_employee_id = target_employee_id
    shift.handover_notes = notes

    await db.commit()
    await db.refresh(shift)
    return shift


# ── SHIFT-005: closing report and financial reconciliation ───────────────────


async def get_shift_closing_summary(
    db: AsyncSession,
    organization_id: int,
    shift_id: int,
) -> dict:
    """Compile token count, cash collections, online payments, and expected cash."""
    shift = await get_shift(db, organization_id, shift_id)

    # Payments associated directly with this shift
    payments = (
        await db.execute(
            select(Payment).where(
                and_(
                    Payment.organization_id == organization_id,
                    Payment.counter_shift_id == shift_id,
                )
            )
        )
    ).scalars().all()

    total_cash = Decimal("0.00")
    total_online = Decimal("0.00")
    total_waived = Decimal("0.00")

    for p in payments:
        method = (p.payment_method or "").lower()
        stat = (p.status or "").lower()
        if method == "cash" and stat in ("paid", "cash_collected"):
            total_cash += p.amount
        elif stat in ("waived",) or method == "waived":
            total_waived += p.amount
        elif stat in ("paid",) and method != "cash":
            total_online += p.amount

    # Tokens served at this counter today
    tokens = (
        await db.execute(
            select(Token).where(
                and_(
                    Token.organization_id == organization_id,
                    Token.counter_id == shift.counter_id,
                    func.date(Token.created_at) == shift.shift_date,
                )
            )
        )
    ).scalars().all()

    status_counts: dict[str, int] = {}
    for t in tokens:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1

    expected_cash = shift.opening_cash + total_cash

    return {
        "shift_id": shift.id,
        "counter_id": shift.counter_id,
        "shift_date": shift.shift_date,
        "opening_cash": shift.opening_cash,
        "total_tokens_served": len(tokens),
        "total_cash_collected": total_cash,
        "total_online_collected": total_online,
        "total_waived_amount": total_waived,
        "expected_cash": expected_cash,
        "status_counts": status_counts,
    }


async def request_close_shift(
    db: AsyncSession,
    employee: Employee,
    shift_id: int,
    counted_cash: Decimal,
    remarks: Optional[str] = None,
) -> CounterShift:
    """Operator counts cash and submits shift closing report (SHIFT-005)."""
    shift = await get_shift(db, employee.organization_id, shift_id)
    if shift.employee_id != employee.id:
        raise ValueError("Only the shift operator can submit the closing report")
    if shift.status not in (CounterShiftStatus.ACTIVE, CounterShiftStatus.PAUSED):
        raise ValueError("Only an active or paused shift can be closed")

    summary = await get_shift_closing_summary(db, employee.organization_id, shift_id)

    expected = summary["expected_cash"]
    variance = counted_cash - expected

    shift.status = CounterShiftStatus.CLOSE_REQUESTED
    shift.closed_at = _utcnow()
    shift.closing_cash_counted = counted_cash
    shift.closing_cash_expected = expected
    shift.closing_cash_variance = variance
    shift.total_tokens_served = summary["total_tokens_served"]
    shift.total_cash_collected = summary["total_cash_collected"]
    shift.total_online_collected = summary["total_online_collected"]
    shift.total_waived_amount = summary["total_waived_amount"]
    shift.closing_remarks = (remarks or "").strip() or None

    # CASH-001..002: Record CashSession discrepancy status
    cash_session = (
        await db.execute(
            select(CashSession).where(CashSession.counter_shift_id == shift.id)
        )
    ).scalar_one_or_none()
    c_status = CashSessionStatus.MATCHED.value if variance == Decimal("0.00") else (
        CashSessionStatus.SHORTAGE.value if variance < Decimal("0.00") else CashSessionStatus.EXCESS.value
    )
    if cash_session:
        cash_session.counted_cash = counted_cash
        cash_session.discrepancy = variance
        cash_session.status = c_status
        cash_session.closed_at = _utcnow()
        cash_session.notes = remarks
    else:
        cash_session = CashSession(
            organization_id=employee.organization_id,
            counter_shift_id=shift.id,
            counter_id=shift.counter_id,
            employee_id=employee.id,
            starting_float=shift.opening_cash,
            collected_cash=summary["total_cash_collected"],
            counted_cash=counted_cash,
            discrepancy=variance,
            status=c_status,
            notes=remarks,
            closed_at=_utcnow(),
        )
        db.add(cash_session)

    # FIN-003: Proportional cash discrepancy evaluation
    await financial_risk_service.evaluate_cash_discrepancy_risk(
        db, employee.organization_id, shift.id, variance
    )

    await db.commit()
    await db.refresh(shift)
    return shift


# ── SHIFT-006: head reconciliation review & sign-off ─────────────────────────


async def reconcile_shift(
    db: AsyncSession,
    organization_id: int,
    shift_id: int,
    action: str,
    notes: Optional[str] = None,
    decided_by_type: str = "organization",
    decided_by_id: Optional[int] = None,
) -> CounterShift:
    """Head reviews closing report: approve, request correction, record discrepancy or escalate."""
    shift = await get_shift(db, organization_id, shift_id)
    if shift.status not in (CounterShiftStatus.CLOSE_REQUESTED, CounterShiftStatus.RECONCILING):
        raise ValueError("Only a shift with closing requested can be reconciled")

    now = _utcnow()
    clean_notes = (notes or "").strip()

    if action == "approve":
        shift.status = CounterShiftStatus.CLOSED
        shift.reconciliation_status = "approved"
        shift.reconciled_at = now
        shift.reconciled_by_type = decided_by_type
        shift.reconciled_by_id = decided_by_id
        shift.reconciliation_notes = clean_notes or "Reconciliation approved by head"
    elif action == "request_correction":
        # Stays active or reverts to active so operator can recount and resubmit
        shift.status = CounterShiftStatus.ACTIVE
        shift.reconciliation_status = "correction_requested"
        shift.reconciliation_notes = clean_notes or "Cash recount / correction requested"
    elif action == "record_discrepancy":
        shift.status = CounterShiftStatus.CLOSED
        if (shift.closing_cash_variance or Decimal("0.00")) < Decimal("0.00"):
            shift.reconciliation_status = "shortage_recorded"
        else:
            shift.reconciliation_status = "excess_recorded"
        shift.reconciled_at = now
        shift.reconciled_by_type = decided_by_type
        shift.reconciled_by_id = decided_by_id
        shift.reconciliation_notes = clean_notes or "Discrepancy recorded"
    elif action == "escalate":
        shift.reconciliation_status = "escalated"
        shift.reconciliation_notes = clean_notes or "Reconciliation escalated for audit"
    else:
        raise ValueError(f"Unknown reconciliation action: {action}")

    await db.commit()
    await db.refresh(shift)
    return shift


async def adjust_reconciled_shift(
    db: AsyncSession,
    organization_id: int,
    shift_id: int,
    adjustment_amount: Decimal,
    reason: str,
    decided_by_type: str = "organization",
    decided_by_id: Optional[int] = None,
) -> CounterShift:
    """Authorized adjustment to a closed/reconciled shift (SHIFT-006)."""
    if not (reason or "").strip():
        raise ValueError("An adjustment reason is mandatory")

    shift = await get_shift(db, organization_id, shift_id)
    if shift.status != CounterShiftStatus.CLOSED:
        raise ValueError("Only a closed, reconciled shift can be adjusted")

    shift.adjustment_amount = adjustment_amount
    shift.adjustment_reason = reason.strip()
    shift.reconciliation_status = "adjusted"

    # LED-003: Post adjustment entry to immutable ledger
    if adjustment_amount != Decimal("0.00"):
        await ledger_service.post_adjustment(
            db=db,
            organization_id=organization_id,
            account=LedgerAccount.CASH,
            direction=LedgerDirection.CREDIT if adjustment_amount < Decimal("0.00") else LedgerDirection.DEBIT,
            amount=abs(adjustment_amount),
            reason=reason.strip(),
            actor_type=decided_by_type,
            actor_id=decided_by_id or 0,
            counter_shift_id=shift.id,
        )

    await db.commit()
    await db.refresh(shift)
    return shift


# ── PHY-001..004: assisted physical / walk-in booking ─────────────────────────


async def search_walkin_customers(
    db: AsyncSession,
    organization_id: int,
    phone: str,
) -> list[dict]:
    """Search registered users by mobile for assisted walk-in desk (PHY-001)."""
    clean_phone = phone.strip()
    if not clean_phone:
        return []
    stmt = (
        select(User)
        .where(User.mobile.like(f"%{clean_phone}%"))
        .limit(10)
    )
    users = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": u.id,
            "name": u.name,
            "mobile": u.mobile,
            "email": u.email,
        }
        for u in users
    ]


async def book_walkin_token(
    db: AsyncSession,
    employee: Employee,
    data: dict,
) -> tuple[Token, Payment]:
    """Assisted physical counter walk-in booking (PHY-001..004).

    Requires active approved shift on the selected counter (SHIFT-003).
    Validates payment permissions, registers cash/waiver/free payment records,
    and returns token + payment.
    """
    from app.services import token_service
    from app.services.form_service import create_token_form_snapshot, get_active_form_for_service, validate_form_submission

    counter_id = int(data["counter_id"])
    service_id = int(data["service_id"])
    customer_name = data["customer_name"].strip()
    customer_phone = data["customer_phone"].strip()

    # SHIFT-003: Must have an ACTIVE shift on this counter
    shift = await require_active_shift(
        db, employee.organization_id, counter_id, employee.id
    )

    # Verify service
    service = await db.get(Service, service_id)
    if not service or service.organization_id != employee.organization_id:
        raise ValueError("Service not found")
    if not service.is_active:
        raise ValueError("Service is currently inactive")

    # SHIFT-002/003: the head-approved shift scope bounds what the desk may
    # issue. An empty scope means "all requested services".
    if shift.approved_services and service_id not in shift.approved_services:
        raise PermissionError(
            "This service is outside the services approved for the active shift"
        )

    payment_method = (data.get("payment_method") or "cash").lower()
    waiver_reason = (data.get("waiver_reason") or "").strip()
    waiver_approved_by = (data.get("waiver_approved_by") or "").strip()
    waiver_evidence = (data.get("waiver_evidence") or "").strip()

    price = Decimal(str(service.price or "0.00"))

    # PHY-002: Options: CASH_COLLECTED, CASH_PENDING, PAY_AT_COUNTER, FREE, WAIVED, ONLINE
    if price == Decimal("0.00"):
        method_stored = "free"
        status_stored = "free"
    elif payment_method == "waived":
        # PHY-003: Waiver requires reason
        if not waiver_reason:
            raise ValueError("A waiver reason is required for fee waiver")
        # PHY-003: above the organization-configured policy a waiver also
        # needs supervisor approval and supporting evidence. Policy keys live
        # in the org settings store: `waiver_approval_threshold` (amount above
        # which approval is needed) and `waiver_requires_approval` ("true" =
        # always supervised). Absent policy = reason-only waiver.
        approval_needed = (
            await _org_setting(db, employee.organization_id, "waiver_requires_approval")
            or ""
        ).lower() == "true"
        threshold_raw = await _org_setting(
            db, employee.organization_id, "waiver_approval_threshold"
        )
        if threshold_raw:
            try:
                threshold = Decimal(str(threshold_raw))
            except InvalidOperation:
                threshold = None
            if threshold is not None and price > threshold:
                approval_needed = True
        if approval_needed:
            if not waiver_approved_by:
                raise PermissionError(
                    "This waiver exceeds the configured policy and requires supervisor approval"
                )
            if not waiver_evidence:
                raise ValueError(
                    "Supporting evidence is required for a supervised waiver"
                )
        method_stored = "waived"
        status_stored = "waived"
    elif payment_method == "cash":
        # Check if shift allows collecting cash payments
        if not shift.payment_permitted:
            raise ValueError("This shift is not authorized to collect cash payments")
        method_stored = "cash"
        status_stored = "cash_collected"
    elif payment_method == "pay_at_counter":
        method_stored = "cash"
        status_stored = "pay_at_counter"
    elif payment_method == "online":
        method_stored = "online"
        status_stored = "pending"
    else:
        raise ValueError(f"Unsupported walk-in payment method: {payment_method}")

    # FIN-001: organization payment-method policy enforcement. Online
    # walk-ins are certified through Razorpay checkout afterwards, so they
    # are validated against the "razorpay" policy entry (bypass prevention).
    # Free services (price == 0) are not subject to the policy.
    if price > Decimal("0.00"):
        from app.services import payment_policy_service
        await payment_policy_service.ensure_method_allowed(
            db,
            employee.organization_id,
            payment_policy_service.WALKIN_METHOD_TO_POLICY[payment_method],
        )

    # Check if phone matches an existing customer
    user_stmt = select(User).where(User.mobile == customer_phone).limit(1)
    existing_user = (await db.execute(user_stmt)).scalar_one_or_none()
    user_id = existing_user.id if existing_user else None

    # Handle custom form answers if dynamic forms are configured for this service
    active_form = await get_active_form_for_service(db, service_id)
    answers_input = data.get("answers") or {}
    if active_form:
        validate_form_submission(active_form, answers_input)

    # Book the token using shared booking engine with walk_in source
    token = await token_service.book_token(
        db=db,
        org_id=employee.organization_id,
        user_id=user_id,
        service_id=service_id,
        counter_id=counter_id,
        guest_name=customer_name if not user_id else None,
        guest_mobile=customer_phone if not user_id else None,
        booking_source=BookingSource.WALK_IN,
        client_reference=data.get("client_reference"),
    )

    # Attach form snapshot if form was present
    if active_form:
        await create_token_form_snapshot(db, token.id, active_form, answers_input)

    # Record the payment row (PHY-004: cash collected is revenue immediately but reconciled at shift close)
    payment = Payment(
        token_id=token.id,
        user_id=user_id,
        organization_id=employee.organization_id,
        counter_shift_id=shift.id,
        amount=price,
        currency="INR",
        payment_method=method_stored,
        status=status_stored,
        recorded_by_type="employee",
        recorded_by_id=employee.id,
        waiver_reason=waiver_reason or None,
        waiver_approved_by=waiver_approved_by or None,
        waiver_evidence=waiver_evidence or None,
        paid_at=_utcnow() if status_stored in ("cash_collected", "free", "waived") else None,
    )
    db.add(payment)
    await db.flush()

    # LED-001..002: Append-only double entry for counter cash collected
    if status_stored == "cash_collected" and price > Decimal("0.00"):
        await ledger_service.post_double_entry(
            db=db,
            organization_id=employee.organization_id,
            debit_account=LedgerAccount.CASH,
            credit_account=LedgerAccount.REVENUE,
            amount=price,
            currency="INR",
            entry_type=LedgerEntryType.CASH,
            actor_type="employee",
            actor_id=employee.id,
            token_id=token.id,
            payment_id=payment.id,
            counter_shift_id=shift.id,
            source_transaction_ref=f"CSH-TKN-{token.id}",
            description=f"Physical desk cash collected for token #{token.token_number}",
        )

    await db.commit()
    await db.refresh(payment)
    await db.refresh(token)

    # FIN-003: transaction-time risk evaluation (after the atomic commit).
    try:
        if method_stored == "waived":
            await financial_risk_service.evaluate_waiver_risk(
                db, employee.organization_id, payment
            )
        await financial_risk_service.evaluate_volume_risk(db, employee.organization_id)
        await financial_risk_service.evaluate_duplicate_reference_risk(
            db, employee.organization_id, payment=payment, token=token
        )
        organization = await db.get(Organization, employee.organization_id)
        if organization is not None:
            await financial_risk_service.evaluate_after_hours_risk(
                db, organization, _utcnow(), token_id=token.id
            )
        await financial_risk_service.evaluate_payment_token_risk(db, payment, token)
    except Exception:
        logger.exception("FIN-003 risk evaluation failed for walk-in token {}", token.id)

    return token, payment

