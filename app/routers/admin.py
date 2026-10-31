"""
app/routers/admin.py
─────────────────────
Admin-only REST API endpoints (JSON).
GET routes that had path conflicts with views_admin.py have been renamed
to /api/* so that the HTML view router always wins the first-match.
"""

import math
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_admin
from app.schemas.auth import ChangePasswordRequest
from app.schemas.common import APIResponse, MessageResponse, PaginatedResponse
from app.schemas.finance import (
    FinancialRiskFlagRead,
    OrgBankAccountRead,
    PaidBookingHoldRead,
    PaidBookingHoldRequest,
    PaymentPolicyRead,
    PaymentPolicyUpdate,
    PayoutHoldRequest,
    PayoutRead,
    RiskFlagResolveRequest,
)
from app.schemas.organization import OrgRead
from app.services import (
    admin_service,
    audit_service,
    bank_account_service,
    financial_risk_service,
    ledger_service,
    payment_policy_service,
    payout_service,
)
from models.admin import Admin
from models.finance import BankAccountStatus, PayoutStatus
from models.organization import Organization
from models.token import Token

router = APIRouter(dependencies=[Depends(get_current_admin)])


# ── Dashboard (JSON API) ──────────────────────────────────────────────────────
# Renamed from /dashboard to /api/dashboard to avoid shadowing the HTML view.

@router.get("/api/dashboard")
async def dashboard_api(
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    stats = await admin_service.get_dashboard_stats(db)
    return APIResponse(data={"admin": {"id": admin.id, "name": admin.name}, "stats": stats})


# ── Organizations (JSON API) ──────────────────────────────────────────────────
# Renamed from /organizations to /api/organizations.

@router.get("/api/organizations")
async def list_organizations_api(
    status: str = None,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    orgs, total = await admin_service.get_all_organizations(db, status=status, page=page, per_page=per_page)
    import math
    return PaginatedResponse(
        total=total, page=page, per_page=per_page,
        pages=math.ceil(total / per_page),
        data=[OrgRead.model_validate(o) for o in orgs],
    )


@router.post("/organizations/{org_id}/approve", response_model=APIResponse)
async def approve_org(
    org_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        org = await admin_service.approve_organization(db, org_id, admin)
        await audit_service.log_audit_event(
            db,
            action="org.approved",
            actor_type="admin",
            actor_id=admin.id,
            actor_email=admin.email,
            organization_id=org_id,
            target_type="organization",
            target_id=str(org_id),
            details={"org_name": org.name},
        )
        return APIResponse(message="Organization approved", data=OrgRead.model_validate(org))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/organizations/{org_id}/reject", response_model=MessageResponse)
async def reject_org(
    org_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        await admin_service.reject_organization(db, org_id, admin)
        await audit_service.log_audit_event(
            db,
            action="org.rejected",
            actor_type="admin",
            actor_id=admin.id,
            actor_email=admin.email,
            organization_id=org_id,
            target_type="organization",
            target_id=str(org_id),
        )
        return MessageResponse(message="Organization rejected")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/organizations/{org_id}/suspend", response_model=MessageResponse)
async def suspend_org(
    org_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        await admin_service.suspend_organization(db, org_id)
        await audit_service.log_audit_event(
            db,
            action="org.suspended",
            actor_type="admin",
            actor_id=admin.id,
            actor_email=admin.email,
            organization_id=org_id,
            target_type="organization",
            target_id=str(org_id),
        )
        return MessageResponse(message="Organization suspended")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/organizations/{org_id}", response_model=MessageResponse)
async def delete_org(
    org_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        await admin_service.delete_organization(db, org_id)
        await audit_service.log_audit_event(
            db,
            action="org.deleted",
            actor_type="admin",
            actor_id=admin.id,
            actor_email=admin.email,
            organization_id=org_id,
            target_type="organization",
            target_id=str(org_id),
        )
        return MessageResponse(message="Organization deleted")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Users (JSON API) ──────────────────────────────────────────────────────────
# Renamed from /users to /api/users.

@router.get("/api/users")
async def list_users_api(
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select, func
    from models.user import User
    total = (await db.execute(select(func.count()).select_from(User))).scalar()
    result = await db.execute(
        select(User).order_by(User.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )
    users = result.scalars().all()
    from app.schemas.user import UserRead
    import math
    return PaginatedResponse(
        total=total, page=page, per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[UserRead.model_validate(u) for u in users],
    )


@router.post("/users/{user_id}/toggle", response_model=MessageResponse)
async def toggle_user(user_id: int, db: AsyncSession = Depends(get_db)):
    from models.user import User
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = not user.is_active
    await db.commit()
    return MessageResponse(message=f"User {'activated' if user.is_active else 'suspended'}")


# ── Change password (JSON API) ────────────────────────────────────────────────

@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    body: ChangePasswordRequest,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    ok = await admin_service.change_admin_password(db, admin, body.current_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    return MessageResponse(message="Password changed successfully")


# ── Phase 5: Admin Financial Controls & Settlements (FIN-001..004) ────────────

@router.get("/api/finance/payouts", response_model=PaginatedResponse)
async def list_admin_payouts_api(
    org_id: int = None,
    status: str = None,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Finance admin reviews scheduled and past payouts."""
    payouts, total = await payout_service.list_payouts(
        db, organization_id=org_id, status=status, page=page, per_page=per_page
    )
    import math
    return PaginatedResponse(
        total=total,
        page=page,
        per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[PayoutRead.model_validate(p) for p in payouts],
    )


@router.post("/payouts/{payout_id}/hold", response_model=APIResponse)
async def hold_payout_api(
    payout_id: int,
    body: PayoutHoldRequest,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Finance admin places a payout on hold with audit justification."""
    try:
        payout = await payout_service.hold_payout(
            db, payout_id=payout_id, reason=body.reason, held_by_type="admin", held_by_id=admin.id
        )
        return APIResponse(message="Payout placed on hold", data=PayoutRead.model_validate(payout))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/payouts/{payout_id}/release", response_model=APIResponse)
async def release_payout_api(
    payout_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Finance admin releases a held payout."""
    try:
        payout = await payout_service.release_payout(
            db, payout_id=payout_id, released_by_type="admin", released_by_id=admin.id
        )
        return APIResponse(message="Payout released for disbursement", data=PayoutRead.model_validate(payout))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/payouts/{payout_id}/disburse", response_model=APIResponse)
async def disburse_payout_api(
    payout_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Disburse payout and append immutable ledger entry."""
    try:
        payout = await payout_service.disburse_payout(db, payout_id=payout_id)
        return APIResponse(message="Payout disbursed successfully", data=PayoutRead.model_validate(payout))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/bank-accounts/{org_id}/verify", response_model=APIResponse)
async def verify_bank_account_api(
    org_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """FIN-002: Admin reviews and verifies organization bank account details."""
    try:
        acc = await bank_account_service.verify_bank_account(
            db, organization_id=org_id, verified_by_type="admin", verified_by_id=admin.id
        )
        from app.schemas.finance import OrgBankAccountRead
        return APIResponse(message="Bank account verified", data=OrgBankAccountRead.model_validate(acc))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/finance/risk-flags", response_model=PaginatedResponse)
async def list_risk_flags_api(
    org_id: int = None,
    status: str = None,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """FIN-003..004: Inspect proportional financial risk flags."""
    flags, total = await financial_risk_service.list_risk_flags(
        db, organization_id=org_id, status=status, page=page, per_page=per_page
    )
    import math
    return PaginatedResponse(
        total=total,
        page=page,
        per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[FinancialRiskFlagRead.model_validate(f) for f in flags],
    )


@router.post("/risk-flags/{flag_id}/resolve", response_model=APIResponse)
async def resolve_risk_flag_api(
    flag_id: int,
    body: RiskFlagResolveRequest,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """FIN-004: Resolve or dismiss proportional risk flag."""
    try:
        flag = await financial_risk_service.resolve_risk_flag(
            db,
            flag_id=flag_id,
            resolved_by=admin.name or f"Admin #{admin.id}",
            resolution_notes=body.resolution_notes,
            new_status=body.status or "resolved",
        )
        return APIResponse(message="Risk flag resolved", data=FinancialRiskFlagRead.model_validate(flag))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Phase 5: Admin Financial Management (read-only listings & overview) ───────

@router.get("/api/finance/bank-accounts", response_model=PaginatedResponse)
async def list_finance_bank_accounts_api(
    status: str = None,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """FIN-002: Admin review queue of organization bank accounts."""
    accounts, total = await bank_account_service.list_bank_accounts(
        db, status=status, page=page, per_page=per_page
    )
    import math
    return PaginatedResponse(
        total=total,
        page=page,
        per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[OrgBankAccountRead.model_validate(a) for a in accounts],
    )


@router.get("/api/finance/overview", response_model=APIResponse)
async def finance_overview_api(
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """Platform financial overview (LED-004 / FIN-001 review).

    All figures are DERIVED FROM LEDGER ENTRIES via ledger_service
    (per-organization totals); payout counts come from payout_service.
    Overview rows cover organizations that have payouts on record.
    """
    payout_counts: dict[str, int] = {}
    totals: dict[str, int | Decimal] = {}
    for payout_status in PayoutStatus:
        _, sub_payout_total = await payout_service.list_payouts(
            db, status=payout_status.value, page=1, per_page=1
        )
        payout_counts[payout_status.value] = sub_payout_total

    _, flagged_total = await financial_risk_service.list_risk_flags(
        db, status="flagged", page=1, per_page=1
    )
    _, pending_accounts_total = await bank_account_service.list_bank_accounts(
        db, status=BankAccountStatus.PENDING_VERIFICATION.value, page=1, per_page=1
    )

    # Organizations under payout review (most recent first), with
    # ledger-derived totals per organization.
    qty = max(1, min(per_page, 100))
    payouts, _ = await payout_service.list_payouts(
        db, page=page, per_page=qty
    )
    org_ids: list[int] = []
    for p in payouts:
        if p.organization_id not in org_ids:
            org_ids.append(p.organization_id)

    from sqlalchemy import select as _select
    name_of: dict[int, str] = {}
    if org_ids:
        org_rows = (await db.execute(
            _select(Organization.id, Organization.name).where(
                Organization.id.in_(org_ids)
            )
        )).all()
        name_of = {row[0]: row[1] for row in org_rows}

    per_org: list[dict] = []
    totals_keys = (
        "gross_revenue", "cash_collected", "gateway_collected",
        "refunds", "platform_fee", "net_revenue",
    )
    aggregate: dict[str, Decimal] = {k: Decimal("0.00") for k in totals_keys}
    for oid in org_ids:
        t = await ledger_service.get_ledger_totals(db, organization_id=oid)
        row: dict = {"organization_id": oid, "organization_name": name_of.get(oid, f"Org #{oid}")}
        for key in totals_keys:
            value = Decimal(str(t.get(key, "0.00")))
            row[key] = value
            aggregate[key] += value
        per_org.append(row)

    return APIResponse(
        message="Financial overview",
        data={
            "payout_counts": payout_counts,
            "flagged_risk_flags": flagged_total,
            "pending_bank_verifications": pending_accounts_total,
            "total_payouts": sum(payout_counts.values()),
            "aggregate_ledger_totals": aggregate,
            "organizations": per_org,
        },
    )


# ── FIN-001: Payment-method policy & paid-booking pause/resume ───────────────

@router.get("/api/organizations/{org_id}/payment-policy", response_model=APIResponse)
async def get_payment_policy_api(
    org_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Read an organization's permitted payment methods
    (null = unrestricted)."""
    try:
        methods = await payment_policy_service.get_payment_policy(db, org_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return APIResponse(
        data=PaymentPolicyRead(organization_id=org_id, allowed_payment_methods=methods)
    )


@router.put("/api/organizations/{org_id}/payment-policy", response_model=APIResponse)
async def set_payment_policy_api(
    org_id: int,
    body: PaymentPolicyUpdate,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Admin-only write of the permitted payment methods.

    `null` lifts all restrictions; a non-empty list must contain only
    `PaymentMethod` vocabulary values. Audited as `payment.policy.updated`."""
    try:
        organization = await payment_policy_service.set_payment_policy(
            db, org_id, body.allowed_payment_methods, admin
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return APIResponse(
        message="Organization payment policy updated",
        data=PaymentPolicyRead(
            organization_id=organization.id,
            allowed_payment_methods=(
                list(organization.allowed_payment_methods)
                if organization.allowed_payment_methods
                else None
            ),
        ),
    )


@router.post("/tokens/{token_id}/pause", response_model=APIResponse)
async def pause_paid_booking_api(
    token_id: int,
    body: PaidBookingHoldRequest,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Pause a PAID booking (amount_paid > 0, active queue status)
    with a mandatory reason. The token moves to the existing ON_HOLD state;
    operators cannot call/progress/complete it until resumed. Audited."""
    token = await db.get(Token, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    try:
        token = await payment_policy_service.pause_paid_booking(db, token, admin, body.reason)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return APIResponse(
        message="Paid booking paused",
        data=PaidBookingHoldRead.model_validate(token),
    )


@router.post("/tokens/{token_id}/resume", response_model=APIResponse)
async def resume_paid_booking_api(
    token_id: int,
    body: PaidBookingHoldRequest,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """FIN-001: Resume a paused paid booking — restores the operator's
    previous queue status. Audited."""
    token = await db.get(Token, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    try:
        token = await payment_policy_service.resume_paid_booking(db, token, admin, body.reason)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return APIResponse(
        message="Paid booking resumed",
        data=PaidBookingHoldRead.model_validate(token),
    )
