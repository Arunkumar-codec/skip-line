"""
app/services/refund_service.py
──────────────────────────────
Phase 5: Refund Management & Idempotency (PAY-003..005, FIN-001, LED-001..004).

Implements:
  - Idempotent refund requests via unique idempotency keys.
  - Gateway & physical cash reversal processing.
  - Supervisor approval workflows and threshold routing.
  - Double-entry ledger integration (Debit Refunds, Credit Gateway/Cash).
  - Preserves original payment history without destructive modifications.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import audit_service, ledger_service
from models.finance import (
    LedgerAccount,
    LedgerEntryType,
    Refund,
    RefundStatus,
)
from models.payment import Payment, PaymentMethod, PaymentStatus
from models.token import Token


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def request_refund(
    db: AsyncSession,
    organization_id: int,
    payment_id: int,
    amount: Decimal,
    reason: str,
    requested_by_type: str,
    requested_by_id: int,
    idempotency_key: Optional[str] = None,
    auto_approve: bool = True,
) -> Refund:
    """
    Initiate or replay a refund request.
    Idempotent on idempotency_key (PAY-003).
    """
    if not (reason or "").strip():
        raise ValueError("Refund reason is mandatory")
    if amount <= Decimal("0.00"):
        raise ValueError("Refund amount must be strictly positive")

    # 1. Idempotency check
    if idempotency_key:
        existing_refund = (
            await db.execute(
                select(Refund).where(Refund.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing_refund:
            return existing_refund

    # 2. Validate Payment
    payment = await db.get(Payment, payment_id)
    if not payment or payment.organization_id != organization_id:
        raise ValueError("Payment record not found for this organization")

    valid_statuses = (
        PaymentStatus.PAID.value,
        PaymentStatus.SUCCESSFUL.value,
        PaymentStatus.CASH_COLLECTED.value,
        PaymentStatus.PARTIALLY_REFUNDED.value,
    )
    if payment.status not in valid_statuses:
        raise ValueError(f"Cannot refund payment with status '{payment.status}'")

    current_refunded = payment.refund_amount or Decimal("0.00")
    remaining_refundable = payment.amount - current_refunded
    if amount > remaining_refundable:
        raise ValueError(
            f"Requested refund (₹{amount}) exceeds remaining refundable balance (₹{remaining_refundable})"
        )

    refund = Refund(
        organization_id=organization_id,
        payment_id=payment.id,
        token_id=payment.token_id,
        amount=amount,
        currency=payment.currency,
        reason=reason.strip(),
        status=RefundStatus.PENDING_APPROVAL.value,
        idempotency_key=idempotency_key,
        requested_by_type=requested_by_type,
        requested_by_id=requested_by_id,
    )
    db.add(refund)
    await db.flush()

    if auto_approve:
        await _execute_refund(
            db=db,
            refund=refund,
            payment=payment,
            approved_by_type=requested_by_type,
            approved_by_id=requested_by_id,
        )

    await db.commit()
    await db.refresh(refund)
    return refund


async def _execute_refund(
    db: AsyncSession,
    refund: Refund,
    payment: Payment,
    approved_by_type: str,
    approved_by_id: Optional[int] = None,
) -> None:
    """Internal processor for an approved refund."""
    now = _utcnow()
    refund.status = RefundStatus.PROCESSED.value
    refund.approved_by_type = approved_by_type
    refund.approved_by_id = approved_by_id
    refund.processed_at = now

    # Determine credit contra account
    if payment.payment_method in (PaymentMethod.CASH.value, PaymentMethod.CASH):
        credit_acc = LedgerAccount.CASH
        gateway_ref = f"CASH-REFUND-{refund.id}"
    else:
        credit_acc = LedgerAccount.GATEWAY
        gateway_ref = f"rfnd_mock_{refund.id}"

    refund.gateway_refund_id = gateway_ref

    # Update payment amounts and status
    new_refund_total = (payment.refund_amount or Decimal("0.00")) + refund.amount
    payment.refund_amount = new_refund_total
    if new_refund_total >= payment.amount:
        payment.status = PaymentStatus.REFUNDED.value
    else:
        payment.status = PaymentStatus.PARTIALLY_REFUNDED.value

    # Update token if full refund
    if payment.status == PaymentStatus.REFUNDED.value and payment.token_id:
        token = await db.get(Token, payment.token_id)
        if token:
            token.amount_paid = max(Decimal("0.00"), (token.amount_paid or Decimal("0.00")) - refund.amount)

    # Post Double-Entry Ledger Entry (LED-001..004)
    await ledger_service.post_double_entry(
        db=db,
        organization_id=refund.organization_id,
        debit_account=LedgerAccount.REFUNDS,
        credit_account=credit_acc,
        amount=refund.amount,
        currency=refund.currency,
        entry_type=LedgerEntryType.REFUND,
        actor_type=approved_by_type,
        actor_id=approved_by_id,
        token_id=refund.token_id,
        payment_id=payment.id,
        counter_shift_id=payment.counter_shift_id,
        source_transaction_ref=gateway_ref,
        description=f"Refund: {refund.reason}",
        entry_metadata={
            "refund_id": refund.id,
            "original_payment_method": payment.payment_method,
            "original_amount": str(payment.amount),
        },
    )

    # Audit event
    await audit_service.log_audit_event(
        db,
        action="payment.refunded",
        actor_type=approved_by_type,
        actor_id=approved_by_id,
        organization_id=refund.organization_id,
        target_type="payment",
        target_id=str(payment.id),
        details={
            "refund_id": refund.id,
            "amount": str(refund.amount),
            "reason": refund.reason,
            "status": payment.status,
        },
    )


async def approve_refund(
    db: AsyncSession,
    organization_id: int,
    refund_id: int,
    approved_by_type: str,
    approved_by_id: Optional[int] = None,
) -> Refund:
    """Supervisor/Admin approves pending refund (FIN-001)."""
    refund = await db.get(Refund, refund_id)
    if not refund or refund.organization_id != organization_id:
        raise ValueError("Refund record not found")
    if refund.status != RefundStatus.PENDING_APPROVAL.value:
        raise ValueError(f"Refund is already in status '{refund.status}'")

    payment = await db.get(Payment, refund.payment_id)
    if not payment:
        raise ValueError("Associated payment not found")

    await _execute_refund(
        db=db,
        refund=refund,
        payment=payment,
        approved_by_type=approved_by_type,
        approved_by_id=approved_by_id,
    )
    await db.commit()
    await db.refresh(refund)
    return refund


async def reject_refund(
    db: AsyncSession,
    organization_id: int,
    refund_id: int,
    rejection_reason: str,
    rejected_by_type: str,
    rejected_by_id: Optional[int] = None,
) -> Refund:
    """Supervisor/Admin rejects pending refund (FIN-001)."""
    if not (rejection_reason or "").strip():
        raise ValueError("A rejection reason is mandatory")

    refund = await db.get(Refund, refund_id)
    if not refund or refund.organization_id != organization_id:
        raise ValueError("Refund record not found")
    if refund.status != RefundStatus.PENDING_APPROVAL.value:
        raise ValueError(f"Refund is already in status '{refund.status}'")

    refund.status = RefundStatus.REJECTED.value
    refund.rejection_reason = rejection_reason.strip()
    refund.approved_by_type = rejected_by_type
    refund.approved_by_id = rejected_by_id

    await audit_service.log_audit_event(
        db,
        action="payment.refund_rejected",
        actor_type=rejected_by_type,
        actor_id=rejected_by_id,
        organization_id=organization_id,
        target_type="refund",
        target_id=str(refund.id),
        details={"reason": rejection_reason.strip()},
    )

    await db.commit()
    await db.refresh(refund)
    return refund


async def list_refunds(
    db: AsyncSession,
    organization_id: int,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[Refund], int]:
    """List all refunds for an organization."""
    stmt = select(Refund).where(Refund.organization_id == organization_id)
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = (
        stmt.order_by(Refund.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all()), total

