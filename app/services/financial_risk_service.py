"""
app/services/financial_risk_service.py
──────────────────────────────────────
Phase 5: Proportional Financial Risk Rules & Review (FIN-003..004).

Implements:
  - FIN-003: Automated detection of unusual amounts, refund spikes, cash mismatches,
    waiver spikes, after-hours activity, and payment-token mismatches.
  - FIN-004: Flags trigger proportional administrative review without punitive lockouts.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import audit_service
from models.finance import (
    FinancialRiskFlag,
    Refund,
    RiskFlagStatus,
    RiskSeverity,
)
from models.organization import Organization
from models.payment import Payment, PaymentMethod, PaymentStatus
from models.token import Token
from models.token_service_item import TokenServiceItem


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def flag_risk(
    db: AsyncSession,
    organization_id: int,
    risk_type: str,
    description: str,
    severity: str = RiskSeverity.MEDIUM.value,
    metadata: Optional[dict] = None,
    commit: bool = True,
) -> FinancialRiskFlag:
    """Create a proportional financial risk flag for admin review (FIN-003/004).

    `commit=False` keeps everything inside the caller's transaction for
    flows that must stay atomic (booking/confirmation paths); the caller
    owns the commit then."""
    flag = FinancialRiskFlag(
        organization_id=organization_id,
        risk_type=risk_type,
        severity=severity,
        status=RiskFlagStatus.FLAGGED.value,
        description=description,
        flag_metadata=metadata,
    )
    db.add(flag)
    await db.flush()

    await audit_service.log_audit_event(
        db,
        action="risk.flagged",
        actor_type="system",
        actor_id=None,
        organization_id=organization_id,
        target_type="financial_risk_flag",
        target_id=str(flag.id),
        details={"risk_type": risk_type, "severity": severity, "description": description},
        commit=commit,
    )

    if commit:
        await db.commit()
    await db.refresh(flag)
    return flag


async def evaluate_transaction_risk(
    db: AsyncSession,
    organization_id: int,
    payment_id: int,
    amount: Decimal,
    token_price: Optional[Decimal] = None,
) -> list[FinancialRiskFlag]:
    """Check for single-transaction anomalies."""
    flags = []

    # 1. Unusual transaction amount (> ₹10,000)
    if amount >= Decimal("10000.00"):
        flag = await flag_risk(
            db=db,
            organization_id=organization_id,
            risk_type="unusual_amount",
            description=f"High-value transaction flagged: ₹{amount}",
            severity=RiskSeverity.HIGH.value if amount >= Decimal("50000.00") else RiskSeverity.MEDIUM.value,
            metadata={"payment_id": payment_id, "amount": str(amount)},
        )
        flags.append(flag)

    # 2. Payment-token price mismatch
    if token_price is not None and token_price > Decimal("0.00") and amount != token_price:
        flag = await flag_risk(
            db=db,
            organization_id=organization_id,
            risk_type="payment_mismatch",
            description=f"Payment amount (₹{amount}) differs from service catalogue price (₹{token_price})",
            severity=RiskSeverity.LOW.value,
            metadata={"payment_id": payment_id, "paid": str(amount), "expected": str(token_price)},
        )
        flags.append(flag)

    return flags


async def evaluate_refund_spike_risk(
    db: AsyncSession,
    organization_id: int,
    threshold_count: int = 5,
) -> Optional[FinancialRiskFlag]:
    """Check if refund frequency in the past 24 hours indicates a spike."""
    since = _utcnow() - timedelta(hours=24)
    count_stmt = select(func.count(Refund.id)).where(
        and_(
            Refund.organization_id == organization_id,
            Refund.created_at >= since,
        )
    )
    refund_count = (await db.execute(count_stmt)).scalar() or 0

    if refund_count >= threshold_count:
        return await flag_risk(
            db=db,
            organization_id=organization_id,
            risk_type="refund_spike",
            description=f"Elevated refund volume: {refund_count} refunds processed within 24 hours",
            severity=RiskSeverity.HIGH.value if refund_count >= 15 else RiskSeverity.MEDIUM.value,
            metadata={"24h_refund_count": refund_count},
        )
    return None


async def evaluate_cash_discrepancy_risk(
    db: AsyncSession,
    organization_id: int,
    shift_id: int,
    discrepancy: Decimal,
) -> Optional[FinancialRiskFlag]:
    """Check if shift closing cash discrepancy exceeds threshold (e.g. ₹200)."""
    abs_variance = abs(discrepancy)
    if abs_variance >= Decimal("200.00"):
        direction = "Shortage" if discrepancy < 0 else "Excess"
        return await flag_risk(
            db=db,
            organization_id=organization_id,
            risk_type="cash_mismatch",
            description=f"Shift #{shift_id} closing cash {direction} of ₹{abs_variance}",
            severity=RiskSeverity.HIGH.value if abs_variance >= Decimal("1000.00") else RiskSeverity.MEDIUM.value,
            metadata={"shift_id": shift_id, "discrepancy": str(discrepancy)},
        )
    return None


async def resolve_risk_flag(
    db: AsyncSession,
    flag_id: int,
    resolved_by: str,
    resolution_notes: str,
    new_status: str = RiskFlagStatus.RESOLVED.value,
) -> FinancialRiskFlag:
    """Resolve or dismiss a proportional risk flag (FIN-004)."""
    if not (resolution_notes or "").strip():
        raise ValueError("Resolution notes are mandatory")

    flag = await db.get(FinancialRiskFlag, flag_id)
    if not flag:
        raise ValueError("Risk flag not found")

    flag.status = new_status
    flag.resolved_by = resolved_by.strip()
    flag.resolution_notes = resolution_notes.strip()
    flag.resolved_at = _utcnow()

    await audit_service.log_audit_event(
        db,
        action="risk.resolved",
        actor_type="admin",
        actor_id=None,
        organization_id=flag.organization_id,
        target_type="financial_risk_flag",
        target_id=str(flag.id),
        details={"status": new_status, "notes": resolution_notes.strip()},
    )

    await db.commit()
    await db.refresh(flag)
    return flag


async def list_risk_flags(
    db: AsyncSession,
    organization_id: Optional[int] = None,
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[FinancialRiskFlag], int]:
    """Retrieve filtered paginated financial risk flags."""
    conditions = []
    if organization_id:
        conditions.append(FinancialRiskFlag.organization_id == organization_id)
    if status:
        conditions.append(FinancialRiskFlag.status == status)

    base = select(FinancialRiskFlag)
    if conditions:
        base = base.where(and_(*conditions))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    stmt = (
        base.order_by(FinancialRiskFlag.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all()), total


# ══════════════════════════════════════════════════════════════════════════════
# FIN-003 (October 2026 closure): remaining risk rules —
# unusual volume, duplicate references, after-hours activity, waiver
# irregularities, price changes, payment-token consistency.
# Thresholds below are PROVISIONAL (no values are stated in
# MASTER_REQUIREMENTS); they follow the same conservative pattern as the
# pre-existing rules (₹10,000 unusual amount / 5 refunds / ₹200 cash) and
# are recorded in OPEN_DECISIONS as pending a business decision.
# ══════════════════════════════════════════════════════════════════════════════

VOLUME_THRESHOLD_24H = 100  # provisional


async def evaluate_volume_risk(
    db: AsyncSession,
    organization_id: int,
    commit: bool = True,
) -> Optional[FinancialRiskFlag]:
    """Unusual transaction volume: ≥ 100 payment records for the
    organization within the past 24 hours (provisional threshold)."""
    since = _utcnow() - timedelta(hours=24)
    count_stmt = select(func.count(Payment.id)).where(
        and_(
            Payment.organization_id == organization_id,
            Payment.created_at >= since,
        )
    )
    count = (await db.execute(count_stmt)).scalar() or 0
    if count < VOLUME_THRESHOLD_24H:
        return None
    return await flag_risk(
        db=db,
        organization_id=organization_id,
        risk_type="unusual_volume",
        description=f"Elevated transaction volume: {count} payment records within 24 hours",
        severity=RiskSeverity.MEDIUM.value,
        metadata={"24h_count": count, "threshold": VOLUME_THRESHOLD_24H},
        commit=commit,
    )


async def evaluate_duplicate_reference_risk(
    db: AsyncSession,
    organization_id: int,
    payment: Optional[Payment] = None,
    token: Optional[Token] = None,
    commit: bool = True,
) -> Optional[FinancialRiskFlag]:
    """Duplicate references, organization-scoped:
    - the same `razorpay_order_id` on more than one payment record; or
    - the same `tokens.client_reference` reused on a second token.
    Either duplicate leg raises one `duplicate_reference` flag."""
    legs: list[str] = []
    if payment is not None and payment.razorpay_order_id:
        dup_payments = (await db.execute(
            select(func.count(Payment.id)).where(
                Payment.razorpay_order_id == payment.razorpay_order_id,
                Payment.organization_id == organization_id,
                Payment.id != payment.id,
            )
        )).scalar() or 0
        if dup_payments > 0:
            legs.append(f"order_id {payment.razorpay_order_id} reused by {dup_payments} other payment record(s)")
    if token is not None and token.client_reference:
        dup_tokens = (await db.execute(
            select(func.count(Token.id)).where(
                Token.client_reference == token.client_reference,
                Token.organization_id == organization_id,
                Token.id != token.id,
            )
        )).scalar() or 0
        if dup_tokens > 0:
            legs.append(f"client_reference {token.client_reference!r} reused by {dup_tokens} other token(s)")
    if not legs:
        return None
    return await flag_risk(
        db=db,
        organization_id=organization_id,
        risk_type="duplicate_reference",
        description="; ".join(legs),
        severity=RiskSeverity.HIGH.value,
        metadata={
            "payment_id": payment.id if payment is not None else None,
            "token_id": token.id if token is not None else None,
            "razorpay_order_id": payment.razorpay_order_id if payment is not None else None,
            "client_reference": token.client_reference if token is not None else None,
        },
        commit=commit,
    )


async def evaluate_after_hours_risk(
    db: AsyncSession,
    organization: "Organization",
    occurred_at: datetime,
    token_id: Optional[int] = None,
    commit: bool = True,
) -> Optional[FinancialRiskFlag]:
    """After-hours activity: the transaction timestamp falls outside the
    organization's published operating hours (resolved in the org's IANA
    timezone via schedule_service, exception-aware). Skipped entirely for
    legacy tenants that have no published schedule at all
    (`is_legacy_day_queue`)."""
    from app.services import schedule_service

    tz_name = organization.timezone or "Asia/Kolkata"
    local_dt = occurred_at.astimezone(schedule_service._zone(tz_name))
    availability = await schedule_service.resolve_day_availability(
        db, organization, local_dt.date()
    )
    if availability.is_legacy_day_queue:
        return None  # no published schedule to compare against

    if availability.opens_at is None and availability.closes_at is None:
        if availability.state in (schedule_service.AvailabilityState.CLOSED, schedule_service.AvailabilityState.HOLIDAY):
            inside = False
        elif availability.state == schedule_service.AvailabilityState.NOT_OPEN:
            inside = False  # published calendar but no hours on this weekday
        else:
            return None  # ambiguous (available around the clock) — skip
    else:
        opens, closes = availability.opens_at, availability.closes_at
        local_time = local_dt.time()
        if opens > closes:  # defensive: overnight window (rows are validated open<close)
            inside = local_time >= opens or local_time <= closes
        else:
            inside = opens <= local_time <= closes

    if inside:
        return None
    return await flag_risk(
        db=db,
        organization_id=organization.id,
        risk_type="after_hours",
        description=(
            f"Transaction recorded outside published operating hours "
            f"({local_dt.isoformat()} local; day state={availability.state.value})"
        ),
        severity=RiskSeverity.MEDIUM.value,
        metadata={
            "occurred_at": occurred_at.isoformat(),
            "local_time": local_dt.time().isoformat(),
            "day_state": availability.state.value,
            "opens_at": availability.opens_at.isoformat() if availability.opens_at else None,
            "closes_at": availability.closes_at.isoformat() if availability.closes_at else None,
            "token_id": token_id,
        },
        commit=commit,
    )


async def evaluate_waiver_risk(
    db: AsyncSession,
    organization_id: int,
    payment: Payment,
    commit: bool = True,
) -> Optional[FinancialRiskFlag]:
    """Waiver irregularity: every fee waiver (method == 'waived') is surfaced
    for administrative review — the update-path already enforces reason /
    supervised approval per the org's configured policy; no amount threshold
    is invented here (none is stated in the authoritative requirements)."""
    if payment.payment_method != PaymentMethod.WAIVED.value:
        return None
    return await flag_risk(
        db=db,
        organization_id=organization_id,
        risk_type="waiver_irregularity",
        description=f"Fee waiver on payment #{payment.id} (₹{payment.amount}) requires review",
        severity=RiskSeverity.LOW.value,
        metadata={
            "payment_id": payment.id,
            "token_id": payment.token_id,
            "amount": str(payment.amount),
            "waiver_reason": payment.waiver_reason,
            "waiver_approved_by": payment.waiver_approved_by,
            "waiver_evidence": payment.waiver_evidence,
        },
        commit=commit,
    )


ACTIVE_TOKEN_STATUSES = ("waiting", "called", "in_progress", "on_hold")


async def evaluate_price_change_risk(
    db: AsyncSession,
    organization_id: int,
    service_id: int,
    new_total_price: Optional[Decimal] = None,
    new_sub_price: Optional[Decimal] = None,
    sub_service_id: Optional[int] = None,
    commit: bool = True,
) -> Optional[FinancialRiskFlag]:
    """Price change: a catalogue price was changed while active tokens still
    reference the service with a different price snapshot.
    - base services: snapshot is `tokens.amount_paid` vs `new_total_price`;
    - sub-services: snapshot is `token_services.price` rows for the sub.
    No frequency/quantity threshold is invented — any mismatching active
    token surfaces one flag."""
    if new_total_price is not None:
        stale = (await db.execute(
            select(func.count(Token.id)).where(
                Token.organization_id == organization_id,
                Token.service_id == service_id,
                Token.status.in_(ACTIVE_TOKEN_STATUSES),
                Token.amount_paid.isnot(None),
                Token.amount_paid != new_total_price,
            )
        )).scalar() or 0
        if stale == 0:
            return None
        return await flag_risk(
            db=db,
            organization_id=organization_id,
            risk_type="price_change",
            description=(
                f"Service #{service_id} price changed to ₹{new_total_price} while "
                f"{stale} active token(s) still snapshot a different price"
            ),
            severity=RiskSeverity.MEDIUM.value,
            metadata={
                "service_id": service_id,
                "new_price": str(new_total_price),
                "active_tokens_affected": stale,
            },
            commit=commit,
        )
    if new_sub_price is not None:
        stale = (await db.execute(
            select(func.count(Token.id)).distinct().where(
                Token.organization_id == organization_id,
                Token.status.in_(ACTIVE_TOKEN_STATUSES),
                Token.id == TokenServiceItem.token_id,
                TokenServiceItem.sub_service_id == sub_service_id,
                TokenServiceItem.price != new_sub_price,
            )
        )).scalar() or 0
        if stale == 0:
            return None
        return await flag_risk(
            db=db,
            organization_id=organization_id,
            risk_type="price_change",
            description=(
                f"Sub-service #{sub_service_id} price changed to ₹{new_sub_price} while "
                f"{stale} active token(s) still snapshot a different price"
            ),
            severity=RiskSeverity.MEDIUM.value,
            metadata={
                "service_id": service_id,
                "sub_service_id": sub_service_id,
                "new_price": str(new_sub_price),
                "active_tokens_affected": stale,
            },
            commit=commit,
        )
    return None


_SUCCESS_CONFIRMED_STATUSES = (
    PaymentStatus.PAID.value,
    PaymentStatus.SUCCESSFUL.value,
    PaymentStatus.CASH_COLLECTED.value,
    PaymentStatus.FREE.value,
    PaymentStatus.WAIVED.value,
)


async def evaluate_payment_token_risk(
    db: AsyncSession,
    payment: Payment,
    token: Token,
    commit: bool = True,
) -> Optional[FinancialRiskFlag]:
    """Payment-token consistency (organization-scoped). Flags the first leg
    that violates invariants:
      a) the payment's organization_id differs from its token's;
      b) a new confirmation on a token already in a terminal state
         (cancelled / completed / skipped);
      c) more than one successful payment record for the same token;
      d) the confirmed amount differs from the token's booked price
         snapshot (`amount_paid`)."""
    legs: list[tuple[str, str, RiskSeverity]] = []  # (code, description)

    if payment.organization_id != token.organization_id:
        legs.append((
            "organization_mismatch",
            f"Payment #{payment.id} (org {payment.organization_id}) does not match "
            f"token #{token.id} (org {token.organization_id})",
            RiskSeverity.HIGH.value,
        ))

    if token.status in ("cancelled", "completed", "skipped"):
        legs.append((
            "terminal_token_confirmation",
            f"Payment confirmation attempt on token #{token.id} already in "
            f"terminal state '{token.status}'",
            RiskSeverity.HIGH.value,
        ))

    multiple = (await db.execute(
        select(func.count(Payment.id)).where(
            Payment.token_id == token.id,
            Payment.id != payment.id,
            Payment.status.in_(_SUCCESS_CONFIRMED_STATUSES),
        )
    )).scalar() or 0
    if multiple > 0:
        legs.append((
            "multiple_successful_payments",
            f"{multiple} other successful payment record(s) already exist for token #{token.id}",
            RiskSeverity.HIGH.value,
        ))

    if (token.amount_paid or Decimal("0.00")) > 0 and Decimal(str(payment.amount)) != Decimal(str(token.amount_paid)):
        legs.append((
            "amount_snapshot_mismatch",
            f"Payment amount ₹{payment.amount} differs from token #{token.id} "
            f"booked price snapshot ₹{token.amount_paid}",
            RiskSeverity.LOW.value,
        ))

    if not legs:
        return None
    severity_rank = {s.value: i for i, s in enumerate(RiskSeverity)}
    top = max(legs, key=lambda l: severity_rank.get(l[2], 0))
    return await flag_risk(
        db=db,
        organization_id=token.organization_id,
        risk_type="payment_token_mismatch",
        description="; ".join(l[1] for l in legs),
        severity=top[2],
        metadata={
            "payment_id": payment.id,
            "token_id": token.id,
            "legs": [l[0] for l in legs],
        },
        commit=commit,
    )

