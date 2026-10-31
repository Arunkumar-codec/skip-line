"""
app/services/cash_service.py
────────────────────────────
Phase 5: Cash Collection & Shift Reconciliation (CASH-001..003).

Implements:
  - CASH-001: Cash records capturing employee, shift, counter, token, amount,
    time and reconciliation state.
  - CASH-002: Shift closing reports MATCHED, SHORTAGE, EXCESS or PENDING_VERIFICATION.
  - CASH-003: Detailed financial reports by date, service, counter and source.
  - Physical cash denomination calculations.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.services import audit_service, ledger_service
from models.counter import Counter
from models.counter_shift import CounterShift, CounterShiftStatus
from models.employee import Employee
from models.finance import (
    CashSession,
    CashSessionStatus,
    LedgerAccount,
    LedgerEntry,
    LedgerEntryType,
)
from models.payment import Payment, PaymentMethod, PaymentStatus
from models.service import Service
from models.token import Token


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def calculate_denominations_total(denominations: dict[str, int]) -> Decimal:
    """
    Calculate the total value from a physical cash denomination dictionary.
    Keys are denomination amounts (e.g. '500', '200', '100', '50', '20', '10', '5', '2', '1'),
    Values are integer counts.
    """
    total = Decimal("0.00")
    for denom_str, count in (denominations or {}).items():
        try:
            val = Decimal(denom_str)
            cnt = int(count)
            if cnt > 0:
                total += val * cnt
        except (ValueError, TypeError):
            continue
    return total


async def open_cash_session(
    db: AsyncSession,
    organization_id: int,
    counter_id: int,
    employee_id: int,
    starting_float: Decimal,
    counter_shift_id: Optional[int] = None,
    denominations: Optional[dict[str, int]] = None,
    notes: Optional[str] = None,
) -> CashSession:
    """Open a drawer cash session for a counter shift (CASH-001)."""
    session = CashSession(
        organization_id=organization_id,
        counter_id=counter_id,
        employee_id=employee_id,
        counter_shift_id=counter_shift_id,
        starting_float=starting_float,
        collected_cash=Decimal("0.00"),
        status=CashSessionStatus.OPEN.value,
        denominations=denominations,
        notes=notes,
    )
    db.add(session)
    await db.flush()
    return session


async def close_cash_session(
    db: AsyncSession,
    session_id: int,
    counted_cash: Decimal,
    denominations: Optional[dict[str, int]] = None,
    notes: Optional[str] = None,
    requires_verification: bool = False,
) -> CashSession:
    """
    Close cash session and evaluate discrepancy (CASH-002):
      - MATCHED if counted == expected
      - SHORTAGE if counted < expected
      - EXCESS if counted > expected
      - PENDING_VERIFICATION if supervisor review is required
    """
    session = await db.get(CashSession, session_id)
    if not session:
        raise ValueError("Cash session not found")
    if session.status != CashSessionStatus.OPEN.value:
        raise ValueError(f"Cash session is not open (current: {session.status})")

    now = _utcnow()
    expected_cash = session.starting_float + session.collected_cash
    discrepancy = counted_cash - expected_cash

    session.counted_cash = counted_cash
    session.discrepancy = discrepancy
    session.denominations = denominations or session.denominations
    session.notes = notes or session.notes
    session.closed_at = now

    if requires_verification:
        session.status = CashSessionStatus.PENDING_VERIFICATION.value
    elif discrepancy == Decimal("0.00"):
        session.status = CashSessionStatus.MATCHED.value
    elif discrepancy < Decimal("0.00"):
        session.status = CashSessionStatus.SHORTAGE.value
    else:
        session.status = CashSessionStatus.EXCESS.value

    await db.commit()
    await db.refresh(session)
    return session


async def get_financial_breakdown_report(
    db: AsyncSession,
    organization_id: int,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    service_id: Optional[int] = None,
    counter_id: Optional[int] = None,
) -> dict[str, Any]:
    """
    CASH-003: Report gross revenue, refunds, discounts, tax, platform fees,
    net organization revenue, pending/unreconciled cash and payouts by date/service/counter/source.
    """
    # 1. Base query for payments
    pay_conditions = [Payment.organization_id == organization_id]
    if date_from:
        pay_conditions.append(Payment.created_at >= date_from)
    if date_to:
        pay_conditions.append(Payment.created_at <= date_to)

    pay_query = (
        select(Payment)
        .options(selectinload(Payment.token))
        .where(and_(*pay_conditions))
    )
    payments = (await db.execute(pay_query)).scalars().all()

    # Filter in-memory if service_id / counter_id specified on token
    filtered_payments: list[Payment] = []
    for p in payments:
        if service_id and (not p.token or p.token.service_id != service_id):
            continue
        if counter_id and (not p.token or p.token.counter_id != counter_id):
            continue
        filtered_payments.append(p)

    gross_revenue = Decimal("0.00")
    total_refunds = Decimal("0.00")
    cash_collected = Decimal("0.00")
    online_collected = Decimal("0.00")
    pending_cash = Decimal("0.00")
    waived_amount = Decimal("0.00")
    free_count = 0
    paid_count = 0

    breakdown_by_source: dict[str, Decimal] = {
        "online": Decimal("0.00"),
        "kiosk": Decimal("0.00"),
        "assisted_counter": Decimal("0.00"),
        "mobile_app": Decimal("0.00"),
        "unknown": Decimal("0.00"),
    }

    breakdown_by_service: dict[int, dict[str, Any]] = {}

    for p in filtered_payments:
        amt = p.amount or Decimal("0.00")
        ref_amt = p.refund_amount or Decimal("0.00")

        # Booking source
        src = "online"
        if p.token and p.token.booking_source:
            src = str(p.token.booking_source).lower()
        if src not in breakdown_by_source:
            src = "unknown"

        # Service mapping
        svc_id = p.token.service_id if p.token else 0
        if svc_id not in breakdown_by_service:
            breakdown_by_service[svc_id] = {
                "service_id": svc_id,
                "revenue": Decimal("0.00"),
                "token_count": 0,
            }
        breakdown_by_service[svc_id]["token_count"] += 1

        if p.status in (PaymentStatus.PAID.value, PaymentStatus.SUCCESSFUL.value):
            gross_revenue += amt
            online_collected += amt
            paid_count += 1
            breakdown_by_source[src] += amt
            breakdown_by_service[svc_id]["revenue"] += amt
        elif p.status == PaymentStatus.CASH_COLLECTED.value:
            gross_revenue += amt
            cash_collected += amt
            paid_count += 1
            breakdown_by_source[src] += amt
            breakdown_by_service[svc_id]["revenue"] += amt
        elif p.status in (PaymentStatus.PAY_AT_COUNTER.value, PaymentStatus.CASH_PENDING.value):
            pending_cash += amt
        elif p.status == PaymentStatus.WAIVED.value:
            waived_amount += amt
        elif p.status == PaymentStatus.FREE.value:
            free_count += 1
        elif p.status in (PaymentStatus.REFUNDED.value, PaymentStatus.PARTIALLY_REFUNDED.value):
            total_refunds += ref_amt

    # Standard tax (e.g. 18% GST built-in if configured, or 0) and platform fee (e.g. 2%)
    platform_fee = (gross_revenue * Decimal("0.02")).quantize(Decimal("0.01"))
    tax_amount = (platform_fee * Decimal("0.18")).quantize(Decimal("0.01"))
    net_revenue = gross_revenue - total_refunds - platform_fee

    return {
        "gross_revenue": gross_revenue,
        "refunds": total_refunds,
        "discounts": Decimal("0.00"),
        "tax": tax_amount,
        "platform_fees": platform_fee,
        "net_organization_revenue": net_revenue,
        "cash_collected": cash_collected,
        "online_collected": online_collected,
        "pending_cash": pending_cash,
        "waived_amount": waived_amount,
        "paid_tokens_count": paid_count,
        "free_tokens_count": free_count,
        "breakdown_by_source": {k: float(v) for k, v in breakdown_by_source.items()},
        "breakdown_by_service": list(breakdown_by_service.values()),
    }

