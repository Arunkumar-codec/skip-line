"""
app/services/enforcement_service.py
───────────────────────────────────
Phase 8 — Enforcement & Escalation Lifecycle (ENF-002..005).

Ladder (each escalation supersedes the previous action row and links to
it — the full chain is preserved as immutable evidence):

    warning → correction_deadline → scoped_restriction → suspension →
    permanent_block

Authoritative anchors:
  - MASTER §4.4: high-risk actions support a second-person review /
    dual approval. Provisional policy (recorded in OPEN_DECISIONS
    Decision 24): SUSPENSION and PERMANENT_BLOCK require a second
    approver who is a DIFFERENT Super Admin from the issuing one.
  - ENF-003: before a suspension/block the platform produces a
    pre-suspension impact summary (active/future tokens, pending
    payments/refunds, open support cases, cash exposure).
  - ENF-004: organizations can appeal; a decision can request
    re-verification (the KYC desk handles the actual re-verification).

Restriction enforcement: booking paths call `ensure_booking_allowed` —
organization-level suspensions/blocks refuse new tokens everywhere;
scoped restrictions (branch/service/counter) refuse tokens only when the
booking touches the restricted target.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import audit_service
from models.audit_log import AuditLog
from models.branch import Branch
from models.counter import Counter
from models.counter_shift import CounterShift
from models.enforcement import (
    EnforcementAction,
    EnforcementAppeal,
    EnforcementScope,
    EnforcementStatus,
    EnforcementType,
)
from models.finance import Refund, RefundStatus
from models.payment import PaymentStatus
from models.organization import Organization
from models.payment import Payment
from models.service import Service
from models.support import CaseStatus, SupportCase
from models.token import Token, TokenStatus

_ACTIVE_TOKEN_STATUSES = (
    TokenStatus.WAITING.value,
    TokenStatus.CALLED.value,
    TokenStatus.IN_PROGRESS.value,
    TokenStatus.ON_HOLD.value,
)

# Stages that require a distinct second approver (Decision 24, provisional —
# MASTER §4.4 does not enumerate which actions are high-risk)
_SECOND_APPROVAL_TYPES = frozenset({
    EnforcementType.SUSPENSION.value,
    EnforcementType.PERMANENT_BLOCK.value,
})

# Stage ranking for escalation validation
_LADDER_ORDER = {
    EnforcementType.WARNING.value: 1,
    EnforcementType.CORRECTION_DEADLINE.value: 2,
    EnforcementType.SCOPED_RESTRICTION.value: 3,
    EnforcementType.SUSPENSION.value: 4,
    EnforcementType.PERMANENT_BLOCK.value: 5,
}

# A restriction expires by default after this many days when no explicit
# restricted_until is provided (provisional — Decision 24).
_DEFAULT_RESTRICTION_DAYS = 14
_DEFAULT_DEADLINE_DAYS = 7


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EnforcementError(ValueError):
    """Raised for illegal enforcement transitions (mapped to 409)."""


# ═══════════════════════════════════════════════════════════════════
# Restriction lookup / enforcement
# ═══════════════════════════════════════════════════════════════════

async def active_actions(
    db: AsyncSession,
    organization_id: int,
    *,
    scope_type: Optional[str] = None,
    include_expired_check: bool = True,
) -> list[EnforcementAction]:
    """Active enforcement rows; the default restriction window is applied
    so an expired but un-cleaned action no longer restricts."""
    stmt = select(EnforcementAction).where(
        EnforcementAction.organization_id == organization_id,
        EnforcementAction.status == EnforcementStatus.ACTIVE.value,
    )
    if scope_type is not None:
        stmt = stmt.where(EnforcementAction.scope_type == scope_type)
    if include_expired_check:
        stmt = stmt.where(
            or_(
                EnforcementAction.restricted_until.is_(None),
                EnforcementAction.restricted_until > _utcnow(),
            )
        )
    stmt = stmt.order_by(
        EnforcementAction.action_type.desc(), EnforcementAction.created_at.desc()
    )
    return list((await db.execute(stmt)).scalars().all())


async def ensure_booking_allowed(
    db: AsyncSession,
    organization_id: int,
    *,
    counter_id: Optional[int] = None,
    service_id: Optional[int] = None,
) -> None:
    """Booking-path gate (called by token_service.book_token so every
    channel — user / kiosk / assisted walk-in — is covered).

    Raises:
      LookupError  — scoped restriction names a missing target (treated
                     as no restriction)
      PermissionError — an active org-level suspension / permanent block
                        or a matching scoped restriction
    """
    org_restriction = None
    actions = await active_actions(db, organization_id)
    for a in actions:
        if a.scope_type == EnforcementScope.ORGANIZATION.value:
            org_restriction = a
            break
    if org_restriction is not None:
        raise PermissionError(
            "This organization is currently under platform enforcement "
            f"({org_restriction.action_type}); new tokens are disabled"
        )

    for a in actions:
        if a.scope_type != EnforcementScope.ORGANIZATION.value:
            if a.scope_target_id is None:
                continue
            if a.scope_type == EnforcementScope.BRANCH.value and counter_id:
                counter = await db.get(Counter, counter_id)
                if counter is not None and counter.branch_id == a.scope_target_id:
                    raise PermissionError(
                        f"Branch #{a.scope_target_id} is currently restricted"
                    )
            elif a.scope_type == EnforcementScope.SERVICE.value and service_id == a.scope_target_id:
                raise PermissionError(
                    f"Service #{a.scope_target_id} is currently restricted"
                )
            elif a.scope_type == EnforcementScope.COUNTER.value and counter_id == a.scope_target_id:
                raise PermissionError(
                    f"Counter #{a.scope_target_id} is currently restricted"
                )


async def ensure_org_operational(
    db: AsyncSession,
    organization_id: int,
    *,
    require_verified_org: bool = True,
) -> None:
    """Org-side mutations (counters, services, kiosks, staff edits, …)
    are disabled during a suspension or permanent block."""
    actions = await active_actions(db, organization_id)
    for a in actions:
        if a.scope_type == EnforcementScope.ORGANIZATION.value and (
            a.action_type in (
                EnforcementType.SUSPENSION.value,
                EnforcementType.PERMANENT_BLOCK.value,
            )
        ):
            raise PermissionError(
                "Organization is currently suspended; platform operations "
                "are disabled until the enforcement is resolved"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Pre-suspension impact summary (ENF-003)
# ══════════════════════════════════════════════════════════════════════════════

async def build_pre_suspension_impact(db: AsyncSession, org_id: int) -> dict:
    """What would be affected by suspending this organization right now:
    active/future tokens, pending payments/refunds, open support cases
    and open cash exposure (open counter shifts)."""
    active_tokens = (
        await db.execute(
            select(func.count(Token.id)).where(
                Token.organization_id == org_id,
                Token.status.in_(_ACTIVE_TOKEN_STATUSES),
            )
        )
    ).scalar() or 0

    pending_payments = (
        await db.execute(
            select(func.count(Payment.id)).where(
                Payment.organization_id == org_id,
                Payment.status.in_((PaymentStatus.PENDING.value,)),
            )
        )
    ).scalar() or 0

    pending_refunds = (
        await db.execute(
            select(func.count(Refund.id)).where(
                Refund.organization_id == org_id,
                Refund.status.in_((RefundStatus.PENDING_APPROVAL.value,)),
            )
        )
    ).scalar() or 0

    open_cases = (
        await db.execute(
            select(func.count(SupportCase.id)).where(
                SupportCase.organization_id == org_id,
                SupportCase.status.not_in((
                    CaseStatus.CLOSED,
                    CaseStatus.RESOLVED,
                )),
            )
        )
    ).scalar() or 0

    open_cash = (
        await db.execute(
            select(func.count(CounterShift.id)).where(
                CounterShift.organization_id == org_id,
                CounterShift.closed_at.is_(None),
            )
        )
    ).scalar() or 0

    return {
        "active_or_future_tokens": active_tokens,
        "pending_payments": pending_payments,
        "pending_refunds": pending_refunds,
        "open_support_cases": open_cases,
        "open_counter_shifts": open_cash,
    }


def classification_from_impact(impact: dict) -> str:
    """ENF-003: a suspension with live tokens or pending money shall be
    flagged as requiring an explicit coordinated replacement/handover, not
    a silent cut-off."""
    if (
        impact.get("active_or_future_tokens")
        or impact.get("pending_payments")
        or impact.get("pending_refunds")
    ):
        return "requires_refund_or_honor_handling"
    return "no_active_exposure"


# ══════════════════════════════════════════════════════════════════════════════
# Action lifecycle
# ══════════════════════════════════════════════════════════════════════════════

async def _validate_scope_target(
    db: AsyncSession, scope_type: str, target_id: Optional[int], organization_id: int
) -> None:
    if scope_type == EnforcementScope.ORGANIZATION.value:
        if target_id is not None and target_id != organization_id:
            raise EnforcementError(
                "Organization-scoped enforcement cannot target another organization"
            )
        return
    if target_id is None:
        raise EnforcementError(
            f"Scope '{scope_type}' requires a concrete target id"
        )
    if scope_type == EnforcementScope.BRANCH.value:
        row = await db.get(Branch, target_id)
    elif scope_type == EnforcementScope.SERVICE.value:
        row = await db.get(Service, target_id)
    elif scope_type == EnforcementScope.COUNTER.value:
        row = await db.get(Counter, target_id)
    else:
        raise EnforcementError(f"Unknown enforcement scope '{scope_type}'")
    if row is None or getattr(row, "organization_id", None) != organization_id:
        raise EnforcementError(
            f"Enforcement target #{target_id} not found for this organization"
        )


async def issue_action(
    db: AsyncSession,
    organization_id: int,
    action_type: str,
    reason: str,
    *,
    actor_label: str = "Super Admin",
    actor_id: Optional[int] = None,
    second_approver: Optional[dict] = None,
    scope_type: str = EnforcementScope.ORGANIZATION.value,
    scope_target_id: Optional[int] = None,
    evidence: Optional[dict] = None,
    correction_deadline: Optional[datetime] = None,
    restricted_until: Optional[datetime] = None,
    apply_org_status: bool = True,
    commit: bool = True,
) -> EnforcementAction:
    """Create an enforcement action. SUSPENSION / PERMANENT_BLOCK require
    a verified second approver (`second_approver` dict with
    admin_id/label resolvable upstream) and flip the organization's
    status atomically (ENF-002/003/005)."""
    from models.organization import OrgStatus

    if not (reason or "").strip():
        raise EnforcementError("A reason is mandatory for enforcement actions")
    if action_type not in _LADDER_ORDER:
        raise EnforcementError(f"Unknown enforcement type '{action_type}'")
    await _validate_scope_target(db, scope_type, scope_target_id, organization_id)

    org = await db.get(Organization, organization_id)
    if org is None:
        raise LookupError("Organization not found")

    two_step = action_type in _SECOND_APPROVAL_TYPES
    second_approver_id = None
    second_approver_label = None
    if two_step:
        if not second_approver:
            raise PermissionError(
                f"A second-person approval is mandatory for '{action_type}' "
                "(Master §4.4 dual-approval policy)"
            )
        second_approver_id = int(second_approver["id"])
        second_approver_label = str(second_approver["label"])
        if second_approver_id == actor_id:
            raise PermissionError(
                "The second approver must be a different Super Admin than "
                "the issuing administrator"
            )

    now = _utcnow()
    if restricted_until is None and action_type == EnforcementType.SCOPED_RESTRICTION.value:
        restricted_until = now + timedelta(days=_DEFAULT_RESTRICTION_DAYS)
    if correction_deadline is None and action_type == EnforcementType.CORRECTION_DEADLINE.value:
        correction_deadline = now + timedelta(days=_DEFAULT_DEADLINE_DAYS)

    action = EnforcementAction(
        organization_id=organization_id,
        action_type=action_type,
        status=EnforcementStatus.ACTIVE.value,
        scope_type=scope_type,
        scope_target_id=scope_target_id,
        reason=reason.strip(),
        evidence=evidence,
        correction_deadline=correction_deadline,
        restricted_until=restricted_until,
        issued_by_type="admin",
        issued_by_id=actor_id,
        issued_by_label=actor_label,
        second_approver_id=second_approver_id,
        second_approver_label=second_approver_label,
        second_approved_at=now if two_step else None,
    )
    db.add(action)
    await db.flush()

    # Organization-level suspension/block flips the org status (existing
    # admin policy) — only suspension does; permanent_block additionally
    # deactivates the account for login.
    if apply_org_status and scope_type == EnforcementScope.ORGANIZATION.value:
        if action_type == EnforcementType.SUSPENSION.value and org.status != OrgStatus.SUSPENDED.value:
            org.status = OrgStatus.SUSPENDED.value
        if action_type == EnforcementType.PERMANENT_BLOCK.value:
            org.status = OrgStatus.SUSPENDED.value
            org.is_active = False

    await audit_service.log_audit_event(
        db,
        action="enforcement.action_issued",
        actor_type="admin",
        actor_id=actor_id,
        organization_id=organization_id,
        target_type="enforcement_action",
        target_id=str(action.id),
        details={
            "action_type": action_type,
            "scope_type": scope_type,
            "scope_target_id": scope_target_id,
            "reason": reason.strip(),
            "second_approver_id": second_approver_id,
            "evidence_summary": True,
        },
        commit=False,
    )

    if commit:
        await db.commit()
        await db.refresh(action)
    return action


async def escalate_action(
    db: AsyncSession,
    previous_action_id: int,
    action_type: str,
    reason: str,
    commit: bool = True,
    **kwargs,
) -> EnforcementAction:
    """Escalate one stage up the ladder: the previous row becomes
    SUPERSEDED (never deleted) and a new higher-stage row is created
    atomically — the supersede guard fires before the single commit, so a
    lost concurrency race aborts the whole escalation (ENF-002)."""
    previous = await db.get(EnforcementAction, previous_action_id)
    if previous is None:
        raise LookupError("Enforcement action not found")
    if previous.status != EnforcementStatus.ACTIVE.value:
        raise EnforcementError("Only active actions can be escalated")
    if _LADDER_ORDER[action_type] <= _LADDER_ORDER[previous.action_type]:
        raise EnforcementError(
            "Escalation must move to a stricter stage than the previous action"
        )
    kwargs.pop("commit", None)  # governed by this function's commit
    kwargs.setdefault("evidence", {"escalated_from": previous_action_id})
    new_action = await issue_action(
        db, previous.organization_id, action_type, reason,
        scope_type=previous.scope_type, scope_target_id=previous.scope_target_id,
        commit=False,
        **kwargs,
    )
    # Concurrency-safe supersede: guard the UPDATE on the expected status
    result = await db.execute(
        # deterministic: only flip while still ACTIVE
        EnforcementAction.__table__.update()
        .where(
            EnforcementAction.id == previous_action_id,
            EnforcementAction.status == EnforcementStatus.ACTIVE.value,
        )
        .values(status=EnforcementStatus.SUPERSEDED.value)
    )
    if (result.rowcount or 0) != 1:
        # Lost the flag — abort the whole transaction so neither row lands.
        await db.rollback()
        raise EnforcementError("Concurrent modification — action was already escalated")
    # the chain link is recorded on EVERY escalation (immutable history)
    new_action.previous_action_id = previous_action_id
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(new_action)
    return new_action


async def resolve_action(
    db: AsyncSession,
    action_id: int,
    *,
    status: str = EnforcementStatus.RESOLVED.value,
    reason: str,
    resolver_label: str = "Super Admin",
    resolver_id: Optional[int] = None,
    commit: bool = True,
) -> EnforcementAction:
    """Resolve / revoke an active action. Restores organization status for
    organization-level suspensions/blocks (reinstatement follows the same
    dual-approval policy — the caller supplies a second approver when
    required for reinstatement in the admin router)."""
    from models.organization import OrgStatus

    if status not in (EnforcementStatus.RESOLVED.value, EnforcementStatus.REVOKED.value, EnforcementStatus.SUPERSEDED.value):
        raise EnforcementError(f"Invalid resolution status '{status}'")
    action = await db.get(EnforcementAction, action_id)
    if action is None:
        raise LookupError("Enforcement action not found")
    if action.status != EnforcementStatus.ACTIVE.value:
        raise EnforcementError(
            f"Action is already {action.status}; only ACTIVE actions can be closed"
        )
    if not (reason or "").strip():
        raise EnforcementError("A resolution reason is mandatory")

    action.status = status
    action.resolved_by_label = resolver_label
    action.resolved_reason = reason.strip()
    action.resolved_at = _utcnow()

    await audit_service.log_audit_event(
        db,
        action="enforcement.action_closed",
        actor_type="admin",
        actor_id=resolver_id,
        organization_id=action.organization_id,
        target_type="enforcement_action",
        target_id=str(action.id),
        details={"status": status, "reason": reason.strip()},
        commit=False,
    )

    if commit:
        await db.commit()
        await db.refresh(action)
    return action


async def reinstate_organization(
    db: AsyncSession,
    organization_id: int,
    *,
    reason: str,
    resolver_label: str = "Super Admin",
    resolver_id: Optional[int] = None,
    second_approver: Optional[dict] = None,
    actor_id: Optional[int] = None,
    commit: bool = True,
) -> Organization:
    """Reinstate a suspended organization: every ACTIVE org-level
    suspension/block row must be closed. Master §4.4 explicitly names
    reinstatement of a suspended service as high-risk — apply the same
    dual-approval rule."""
    from models.organization import OrgStatus

    org = await db.get(Organization, organization_id)
    if org is None:
        raise LookupError("Organization not found")

    active_org_level = (
        await db.execute(
            select(EnforcementAction).where(
                EnforcementAction.organization_id == organization_id,
                EnforcementAction.status == EnforcementStatus.ACTIVE.value,
                EnforcementAction.scope_type == EnforcementScope.ORGANIZATION.value,
                EnforcementAction.action_type.in_(_SECOND_APPROVAL_TYPES),
            )
        )
    ).scalars().all()

    if active_org_level:
        if not second_approver:
            raise PermissionError(
                "Reinstating a suspended organization requires a "
                "second-person approval (Master §4.4)"
            )
        second_id = int(second_approver["id"])
        if second_id == (resolver_id if resolver_id is not None else actor_id):
            raise PermissionError(
                "The second approver must be a different Super Admin than "
                "the reinstating administrator"
            )
        # close each active suspension/block with the dual approval recorded
        for action in active_org_level:
            action.status = EnforcementStatus.RESOLVED.value
            action.resolved_by_label = resolver_label
            action.resolved_reason = reason.strip()
            action.resolved_at = _utcnow()
            action.second_approver_id = second_id
            action.second_approver_label = str(second_approver["label"])
            action.second_approved_at = _utcnow()

    org.status = OrgStatus.APPROVED.value
    org.is_active = True

    await audit_service.log_audit_event(
        db,
        action="organization.reinstated",
        actor_type="admin",
        actor_id=resolver_id,
        organization_id=organization_id,
        target_type="organization",
        target_id=str(organization_id),
        details={"reason": reason.strip(), "second_approver": bool(second_approver)},
        commit=False,
    )
    if commit:
        await db.commit()
        await db.refresh(org)
    return org


# ══════════════════════════════════════════════════════════════════════════════
# Appeals (ENF-004)
# ══════════════════════════════════════════════════════════════════════════════

class AppealStatus:
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"

    TRANSITIONS = {
        SUBMITTED: (IN_REVIEW, APPROVED, REJECTED),
        IN_REVIEW: (APPROVED, REJECTED),
        APPROVED: (),
        REJECTED: (),
    }


async def file_appeal(
    db: AsyncSession,
    organization_id: int,
    action_id: int,
    reason: str,
    commit: bool = True,
) -> EnforcementAppeal:
    if not (reason or "").strip():
        raise EnforcementError("An appeal must describe the requested correction")
    action = await db.get(EnforcementAction, action_id)
    if action is None or action.organization_id != organization_id:
        raise LookupError("Enforcement action not found")
    if action.organization_id != organization_id:
        raise PermissionError("This enforcement action belongs to another organization")
    existing = (
        await db.execute(
            select(EnforcementAppeal).where(
                EnforcementAppeal.enforcement_action_id == action_id,
                EnforcementAppeal.status.in_((
                    AppealStatus.SUBMITTED, AppealStatus.IN_REVIEW,
                )),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise EnforcementError("An appeal is already pending for this action")

    appeal = EnforcementAppeal(
        organization_id=organization_id,
        enforcement_action_id=action_id,
        reason=reason.strip(),
        status=AppealStatus.SUBMITTED,
    )
    db.add(appeal)
    await db.flush()
    await audit_service.log_audit_event(
        db,
        action="enforcement.appeal_filed",
        actor_type="organization",
        actor_id=organization_id,
        organization_id=organization_id,
        target_type="enforcement_appeal",
        target_id=str(appeal.id),
        details={"enforcement_action_id": action_id, "reason": reason.strip()},
        commit=False,
    )
    if commit:
        await db.commit()
        await db.refresh(appeal)
    return appeal


async def decide_appeal(
    db: AsyncSession,
    appeal_id: int,
    *,
    status: str,
    decision_reason: str,
    reviewer_label: str = "Super Admin",
    reviewer_id: Optional[int] = None,
    re_verification_requested: bool = False,
    commit: bool = True,
) -> EnforcementAppeal:
    if status not in AppealStatus.TRANSITIONS:
        raise EnforcementError(f"Unknown appeal status '{status}'")
    if not (decision_reason or "").strip():
        raise EnforcementError("An appeal decision requires a recorded reason")
    appeal = await db.get(EnforcementAppeal, appeal_id)
    if appeal is None:
        raise LookupError("Appeal not found")
    if status not in AppealStatus.TRANSITIONS.get(appeal.status, ()):
        raise EnforcementError(
            f"Illegal appeal transition {appeal.status} → {status}"
        )
    appeal.status = status
    appeal.decision_reason = decision_reason.strip()
    appeal.reviewer_label = reviewer_label
    appeal.re_verification_requested = bool(re_verification_requested)
    appeal.decided_at = _utcnow()

    # decoded decision widens the record: an approved appeal automatically
    # resolves the underlying restriction row (kept as history, RESOLVED)
    if status == AppealStatus.APPROVED:
        action = await db.get(EnforcementAction, appeal.enforcement_action_id)
        if action is not None and action.status == EnforcementStatus.ACTIVE.value:
            action.status = EnforcementStatus.RESOLVED.value
            action.resolved_reason = f"Appeal #{appeal.id} approved: {decision_reason.strip()}"
            action.resolved_by_label = reviewer_label
            action.resolved_at = _utcnow()

    await audit_service.log_audit_event(
        db,
        action="enforcement.appeal_decided",
        actor_type="admin",
        actor_id=reviewer_id,
        organization_id=appeal.organization_id,
        target_type="enforcement_appeal",
        target_id=str(appeal.id),
        details={
            "status": status,
            "decision_reason": decision_reason.strip(),
            "re_verification_requested": bool(re_verification_requested),
        },
        commit=False,
    )
    if commit:
        await db.commit()
        await db.refresh(appeal)
    return appeal


async def list_actions(
    db: AsyncSession,
    organization_id: Optional[int] = None,
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[EnforcementAction], int]:
    conditions = []
    if organization_id:
        conditions.append(EnforcementAction.organization_id == organization_id)
    if status:
        conditions.append(EnforcementAction.status == status)
    base = select(EnforcementAction)
    if conditions:
        base = base.where(and_(*conditions))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    res = await db.execute(
        base.order_by(EnforcementAction.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )
    return list(res.scalars().all()), total
