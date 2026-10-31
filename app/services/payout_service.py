"""
app/services/payout_service.py
──────────────────────────────
Phase 5: Admin Financial Controls & Settlements (FIN-001).

Implements:
  - Payout generation & scheduling based on net ledger balance.
  - Payout hold and release controls with mandatory audit logging.
  - Gateway settlement tracking.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import audit_service, ledger_service
from models.finance import (
    LedgerAccount,
    LedgerEntryType,
    Payout,
    PayoutStatus,
    Settlement,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def schedule_payout(
    db: AsyncSession,
    organization_id: int,
    amount: Decimal,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    bank_account_id: Optional[int] = None,
) -> Payout:
    """Schedule a payout for an organization."""
    if amount <= Decimal("0.00"):
        raise ValueError("Payout amount must be positive")

    payout = Payout(
        organization_id=organization_id,
        amount=amount,
        currency="INR",
        status=PayoutStatus.SCHEDULED.value,
        period_start=period_start,
        period_end=period_end,
        bank_account_id=bank_account_id,
    )
    db.add(payout)
    await db.commit()
    await db.refresh(payout)
    return payout


async def hold_payout(
    db: AsyncSession,
    payout_id: int,
    reason: str,
    held_by_type: str,
    held_by_id: int,
) -> Payout:
    """Finance admin places a payout on hold (FIN-001)."""
    if not (reason or "").strip():
        raise ValueError("A reason for holding the payout is mandatory")

    payout = await db.get(Payout, payout_id)
    if not payout:
        raise ValueError("Payout not found")
    if payout.status not in (PayoutStatus.SCHEDULED.value, PayoutStatus.PROCESSING.value):
        raise ValueError(f"Cannot hold payout in '{payout.status}' status")

    payout.status = PayoutStatus.HELD.value
    payout.hold_reason = reason.strip()
    payout.held_by_type = held_by_type
    payout.held_by_id = held_by_id

    await audit_service.log_audit_event(
        db,
        action="payout.hold",
        actor_type=held_by_type,
        actor_id=held_by_id,
        organization_id=payout.organization_id,
        target_type="payout",
        target_id=str(payout.id),
        details={"reason": reason.strip(), "amount": str(payout.amount)},
    )

    await db.commit()
    await db.refresh(payout)
    return payout


async def release_payout(
    db: AsyncSession,
    payout_id: int,
    released_by_type: str,
    released_by_id: int,
) -> Payout:
    """Finance admin releases a held payout (FIN-001)."""
    payout = await db.get(Payout, payout_id)
    if not payout:
        raise ValueError("Payout not found")
    if payout.status != PayoutStatus.HELD.value:
        raise ValueError(f"Payout is not held (status: '{payout.status}')")

    payout.status = PayoutStatus.SCHEDULED.value
    old_reason = payout.hold_reason
    payout.hold_reason = None
    payout.held_by_type = None
    payout.held_by_id = None

    await audit_service.log_audit_event(
        db,
        action="payout.release",
        actor_type=released_by_type,
        actor_id=released_by_id,
        organization_id=payout.organization_id,
        target_type="payout",
        target_id=str(payout.id),
        details={"previous_hold_reason": old_reason, "amount": str(payout.amount)},
    )

    await db.commit()
    await db.refresh(payout)
    return payout


async def disburse_payout(
    db: AsyncSession,
    payout_id: int,
    gateway_payout_id: Optional[str] = None,
) -> Payout:
    """Mark payout as paid and append double-entry ledger entry."""
    payout = await db.get(Payout, payout_id)
    if not payout:
        raise ValueError("Payout not found")
    if payout.status != PayoutStatus.SCHEDULED.value:
        raise ValueError(f"Only scheduled payouts can be disbursed (current: '{payout.status}')")

    now = _utcnow()
    payout.status = PayoutStatus.PAID.value
    payout.paid_at = now
    payout.gateway_payout_id = gateway_payout_id or f"payout_{payout.id}"

    # Post Double-Entry Ledger Entry: Debit Payouts, Credit Gateway / Bank
    await ledger_service.post_double_entry(
        db=db,
        organization_id=payout.organization_id,
        debit_account=LedgerAccount.PAYOUTS,
        credit_account=LedgerAccount.GATEWAY,
        amount=payout.amount,
        currency=payout.currency,
        entry_type=LedgerEntryType.PAYOUT,
        actor_type="system",
        actor_id=None,
        source_transaction_ref=payout.gateway_payout_id,
        description=f"Disbursed organization payout #{payout.id}",
    )

    await db.commit()
    await db.refresh(payout)
    return payout


async def list_payouts(
    db: AsyncSession,
    organization_id: Optional[int] = None,
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[Payout], int]:
    """List payouts with optional status and organization filters."""
    conditions = []
    if organization_id:
        conditions.append(Payout.organization_id == organization_id)
    if status:
        conditions.append(Payout.status == status)

    base = select(Payout)
    if conditions:
        base = base.where(and_(*conditions))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    stmt = (
        base.order_by(Payout.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all()), total

