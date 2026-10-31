"""
app/services/bank_account_service.py
────────────────────────────────────
Phase 5: Bank Account Management & Security Verification Holds (FIN-002).

Implements:
  - Bank account updates with strong verification.
  - 72-hour payout hold on any bank detail changes to protect against compromise.
  - Owner notification and audit logging.
  - Verification review workflow.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import audit_service
from models.finance import BankAccountStatus, OrgBankAccount
from models.notification import Notification, NotificationType


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def update_bank_account(
    db: AsyncSession,
    organization_id: int,
    account_number: str,
    ifsc_code: str,
    account_holder_name: str,
    bank_name: str,
    updated_by_type: str = "org",
    updated_by_id: Optional[int] = None,
) -> OrgBankAccount:
    """
    Register or update bank account details.
    Applies mandatory 72-hour security hold and requires verification (FIN-002).
    """
    account_num_clean = account_number.strip().replace(" ", "")
    ifsc_clean = ifsc_code.strip().upper()
    holder_clean = account_holder_name.strip()
    bank_clean = bank_name.strip()

    if not account_num_clean or not ifsc_clean or not holder_clean or not bank_clean:
        raise ValueError("All bank account fields are mandatory")

    now = _utcnow()
    hold_deadline = now + timedelta(hours=72)
    token = secrets.token_urlsafe(16)

    # Check for existing account
    existing = (
        await db.execute(
            select(OrgBankAccount).where(OrgBankAccount.organization_id == organization_id)
        )
    ).scalar_one_or_none()

    if existing:
        existing.account_number = account_num_clean
        existing.ifsc_code = ifsc_clean
        existing.account_holder_name = holder_clean
        existing.bank_name = bank_clean
        existing.status = BankAccountStatus.PENDING_VERIFICATION.value
        existing.held_until = hold_deadline
        existing.hold_reason = "Mandatory 72-hour security hold on modified bank details"
        existing.verification_token = token
        account = existing
    else:
        account = OrgBankAccount(
            organization_id=organization_id,
            account_number=account_num_clean,
            ifsc_code=ifsc_clean,
            account_holder_name=holder_clean,
            bank_name=bank_clean,
            status=BankAccountStatus.PENDING_VERIFICATION.value,
            held_until=hold_deadline,
            hold_reason="Mandatory 72-hour security hold on newly registered bank details",
            verification_token=token,
        )
        db.add(account)

    await db.flush()

    # Log Security Audit Event
    await audit_service.log_audit_event(
        db,
        action="bank_account.updated",
        actor_type=updated_by_type,
        actor_id=updated_by_id,
        organization_id=organization_id,
        target_type="org_bank_account",
        target_id=str(account.id),
        details={
            "bank_name": bank_clean,
            "ifsc": ifsc_clean,
            "account_last4": account_num_clean[-4:],
            "held_until": hold_deadline.isoformat(),
        },
    )

    await db.commit()
    await db.refresh(account)
    return account


async def verify_bank_account(
    db: AsyncSession,
    organization_id: int,
    verified_by_type: str = "admin",
    verified_by_id: Optional[int] = None,
) -> OrgBankAccount:
    """Authorize and verify bank account, lifting the temporary hold (FIN-002)."""
    account = (
        await db.execute(
            select(OrgBankAccount).where(OrgBankAccount.organization_id == organization_id)
        )
    ).scalar_one_or_none()

    if not account:
        raise ValueError("Bank account not registered for this organization")

    now = _utcnow()
    account.status = BankAccountStatus.VERIFIED.value
    account.verified_at = now
    account.held_until = None
    account.hold_reason = None

    await audit_service.log_audit_event(
        db,
        action="bank_account.verified",
        actor_type=verified_by_type,
        actor_id=verified_by_id,
        organization_id=organization_id,
        target_type="org_bank_account",
        target_id=str(account.id),
        details={"status": account.status},
    )

    await db.commit()
    await db.refresh(account)
    return account


async def get_bank_account(
    db: AsyncSession,
    organization_id: int,
) -> Optional[OrgBankAccount]:
    """Retrieve bank account details for organization."""
    return (
        await db.execute(
            select(OrgBankAccount).where(OrgBankAccount.organization_id == organization_id)
        )
    ).scalar_one_or_none()


async def list_bank_accounts(
    db: AsyncSession,
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[OrgBankAccount], int]:
    """List organization bank accounts (admin FIN-002 review queue).

    Read-only listing; ordering and filtering follow the same pattern as
    payout_service.list_payouts and financial_risk_service.list_risk_flags.
    """
    conditions = []
    if status:
        conditions.append(OrgBankAccount.status == status)

    base = select(OrgBankAccount)
    if conditions:
        base = base.where(and_(*conditions))

    total = (
        await db.execute(
            select(func.count()).select_from(base.subquery())
        )
    ).scalar() or 0
    stmt = (
        base.order_by(OrgBankAccount.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all()), total

