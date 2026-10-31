"""
app/services/ledger_service.py
──────────────────────────────
Phase 5: Immutable Double-Entry Ledgering (LED-001..004).

Guarantees:
  - LED-001: Append-only ledger entries for charges, taxes, fees, refunds, cash,
    adjustments, settlements and payouts.
  - LED-002: Every entry links to organization, token, payment, counter shift, actor
    and source transaction ref where applicable.
  - LED-003: Completed financial history is immutable. Adjustments and reversals
    must be posted as new signed entries with recorded reason.
  - LED-004: Dashboard and financial totals are derived from ledger entries and
    reconcile to gateway and cash collections.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.finance import (
    LedgerAccount,
    LedgerDirection,
    LedgerEntry,
    LedgerEntryType,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def post_double_entry(
    db: AsyncSession,
    organization_id: int,
    debit_account: str | LedgerAccount,
    credit_account: str | LedgerAccount,
    amount: Decimal,
    currency: str = "INR",
    entry_type: str | LedgerEntryType = LedgerEntryType.CHARGE,
    actor_type: Optional[str] = None,
    actor_id: Optional[int] = None,
    token_id: Optional[int] = None,
    payment_id: Optional[int] = None,
    counter_shift_id: Optional[int] = None,
    source_transaction_ref: Optional[str] = None,
    description: Optional[str] = None,
    entry_metadata: Optional[dict] = None,
) -> tuple[LedgerEntry, LedgerEntry]:
    """
    Post a balanced double-entry transaction (Debit = Credit).
    Appends two entries atomically into ledger_entries.
    """
    if amount <= Decimal("0.00"):
        raise ValueError("Ledger entry amount must be strictly positive")

    deb_acc = debit_account.value if isinstance(debit_account, LedgerAccount) else str(debit_account)
    crd_acc = credit_account.value if isinstance(credit_account, LedgerAccount) else str(credit_account)
    e_type = entry_type.value if isinstance(entry_type, LedgerEntryType) else str(entry_type)

    now = _utcnow()

    debit_entry = LedgerEntry(
        organization_id=organization_id,
        account=deb_acc,
        entry_type=e_type,
        direction=LedgerDirection.DEBIT.value,
        amount=amount,
        currency=currency,
        token_id=token_id,
        payment_id=payment_id,
        counter_shift_id=counter_shift_id,
        actor_type=actor_type,
        actor_id=actor_id,
        source_transaction_ref=source_transaction_ref,
        description=description,
        entry_metadata=entry_metadata,
        created_at=now,
    )

    credit_entry = LedgerEntry(
        organization_id=organization_id,
        account=crd_acc,
        entry_type=e_type,
        direction=LedgerDirection.CREDIT.value,
        amount=amount,
        currency=currency,
        token_id=token_id,
        payment_id=payment_id,
        counter_shift_id=counter_shift_id,
        actor_type=actor_type,
        actor_id=actor_id,
        source_transaction_ref=source_transaction_ref,
        description=description,
        entry_metadata=entry_metadata,
        created_at=now,
    )

    db.add(debit_entry)
    db.add(credit_entry)
    await db.flush()

    return debit_entry, credit_entry


async def post_adjustment(
    db: AsyncSession,
    organization_id: int,
    account: str | LedgerAccount,
    direction: str | LedgerDirection,
    amount: Decimal,
    reason: str,
    actor_type: str,
    actor_id: int,
    contra_account: Optional[str | LedgerAccount] = None,
    counter_shift_id: Optional[int] = None,
    token_id: Optional[int] = None,
    payment_id: Optional[int] = None,
) -> tuple[LedgerEntry, LedgerEntry]:
    """
    Post an explicit reversal/adjustment entry with mandatory reason and actor (LED-003).
    """
    if not (reason or "").strip():
        raise ValueError("An adjustment reason is mandatory (LED-003)")

    contra = contra_account or LedgerAccount.ADJUSTMENTS
    dir_val = direction.value if isinstance(direction, LedgerDirection) else str(direction)

    if dir_val == LedgerDirection.DEBIT.value:
        return await post_double_entry(
            db=db,
            organization_id=organization_id,
            debit_account=account,
            credit_account=contra,
            amount=amount,
            entry_type=LedgerEntryType.ADJUSTMENT,
            actor_type=actor_type,
            actor_id=actor_id,
            token_id=token_id,
            payment_id=payment_id,
            counter_shift_id=counter_shift_id,
            description=f"Adjustment: {reason.strip()}",
            entry_metadata={"adjustment_reason": reason.strip()},
        )
    else:
        return await post_double_entry(
            db=db,
            organization_id=organization_id,
            debit_account=contra,
            credit_account=account,
            amount=amount,
            entry_type=LedgerEntryType.ADJUSTMENT,
            actor_type=actor_type,
            actor_id=actor_id,
            token_id=token_id,
            payment_id=payment_id,
            counter_shift_id=counter_shift_id,
            description=f"Adjustment: {reason.strip()}",
            entry_metadata={"adjustment_reason": reason.strip()},
        )


async def get_ledger_entries(
    db: AsyncSession,
    organization_id: int,
    account: Optional[str] = None,
    entry_type: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[LedgerEntry], int]:
    """Retrieve filtered paginated journal of ledger entries."""
    conditions = [LedgerEntry.organization_id == organization_id]
    if account:
        conditions.append(LedgerEntry.account == account)
    if entry_type:
        conditions.append(LedgerEntry.entry_type == entry_type)
    if date_from:
        conditions.append(LedgerEntry.created_at >= date_from)
    if date_to:
        conditions.append(LedgerEntry.created_at <= date_to)

    base_query = select(LedgerEntry).where(and_(*conditions))
    count_query = select(func.count()).select_from(base_query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    stmt = (
        base_query.order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all()), total


async def get_ledger_totals(
    db: AsyncSession,
    organization_id: int,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> dict[str, Decimal]:
    """
    Derive institutional financial figures directly from immutable ledger entries (LED-004):
      - Gross revenue (Credit Revenue)
      - Tax collected (Credit Tax Payable)
      - Platform fees (Credit Platform Fee)
      - Refunds (Debit Refunds)
      - Cash collected (Debit Cash)
      - Gateway collected (Debit Gateway)
      - Net organization revenue
    """
    conditions = [LedgerEntry.organization_id == organization_id]
    if date_from:
        conditions.append(LedgerEntry.created_at >= date_from)
    if date_to:
        conditions.append(LedgerEntry.created_at <= date_to)

    async def _sum_account(acc: str, direction: str) -> Decimal:
        stmt = select(func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00"))).where(
            and_(
                *conditions,
                LedgerEntry.account == acc,
                LedgerEntry.direction == direction,
            )
        )
        return Decimal(str((await db.execute(stmt)).scalar() or 0))

    gross_revenue = await _sum_account("revenue", LedgerDirection.CREDIT.value)
    cash_collected = await _sum_account("cash", LedgerDirection.DEBIT.value)
    gateway_collected = await _sum_account("gateway", LedgerDirection.DEBIT.value)
    refunds = await _sum_account("refunds", LedgerDirection.DEBIT.value)
    tax_collected = await _sum_account("tax_payable", LedgerDirection.CREDIT.value)
    platform_fee = await _sum_account("platform_fee", LedgerDirection.CREDIT.value)
    payouts_disbursed = await _sum_account("payouts", LedgerDirection.DEBIT.value)

    net_revenue = gross_revenue - refunds - platform_fee

    return {
        "gross_revenue": gross_revenue,
        "cash_collected": cash_collected,
        "gateway_collected": gateway_collected,
        "refunds": refunds,
        "tax_collected": tax_collected,
        "platform_fee": platform_fee,
        "payouts_disbursed": payouts_disbursed,
        "net_revenue": net_revenue,
    }

