"""
app/services/kyc_service.py
───────────────────────────
Phase 7 — Organization KYC (ORGKYC-001..007).

Distinct from the Phase 2 customer identity desk (identity_service).
Enforced invariants:
- The tiered status taxonomy transitions ONLY through the legal map in
  models/organization_kyc.KycStatus — no transition bypass.
- Configurable document requirements per organization category (default
  matrix in code; per-org overrides ride the existing `settings` table).
- Unverified (non-approved/expired) organizations cannot accept tokens
  (ORGKYC-005) — enforced at booking time via assert_booking_allowed.
- KYC documents (organization_verifications rows) are private: reading
  one always goes through an authorized endpoint that masks document
  numbers and appends an audit event with the reviewer/owner identity
  (ORGKYC-007). Retention: expired documents are purge-able by admins.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit_service import log_audit_event
from models.branch import Branch
from models.organization import Organization
from models.organization_kyc import KycStatus, OrganizationKyc
from models.organization_verification import VerificationStatus  # noqa: F401
from models.organization_verification import OrganizationVerification
from models.setting import Setting

# ORGKYC-003: default required document types per organization category.
DEFAULT_DOC_REQUIREMENTS = {
    "hospital": {"registration_certificate", "medical_licence"},
    "clinic": {"registration_certificate", "medical_licence"},
    "laboratory": {"registration_certificate", "trade_licence"},
    "pharmacy": {"registration_certificate", "drug_licence"},
    "bank": {"registration_certificate", "rbic_licence"},
    "telecom": {"registration_certificate", "nic_licence"},
    "government": {"registration_certificate"},
    "retail": {"registration_certificate", "trade_licence"},
    "government_service": {"registration_certificate"},
    "__default__": {"registration_certificate"},
}

REQUIRED_APPLICATION_FIELDS = (
    "legal_name", "registration_number", "org_category", "registered_address",
    "contact_email", "contact_phone", "representative_name",
    "representative_role", "representative_email", "representative_phone",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expires_in_past(expires_at: Optional[datetime]) -> bool:
    """TZ-safe expiry check (SQLite stores naive values as UTC)."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= _now()


def mask_document_number(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * (len(value) - 4) + value[-4:]


async def get_doc_requirements(
    db: AsyncSession, org_category: Optional[str], organization_id: int
) -> set[str]:
    """Configurable requirements: Setting('kyc_doc_requirements') JSON
    override per org, else the default matrix by category."""
    category = (org_category or "__default__").lower()
    base = set(DEFAULT_DOC_REQUIREMENTS.get(category, DEFAULT_DOC_REQUIREMENTS["__default__"]))
    row = (
        await db.execute(
            select(Setting).where(
                Setting.organization_id == organization_id,
                Setting.key == "kyc_doc_requirements",
            )
        )
    ).scalar_one_or_none()
    if row is not None and row.value:
        try:
            override = set(json.loads(row.value))
            if override:
                return override
        except (ValueError, TypeError):
            pass  # corrupt override falls back to defaults
    return base


async def get_or_create_kyc(
    db: AsyncSession, organization_id: int
) -> OrganizationKyc:
    kyc = (
        await db.execute(
            select(OrganizationKyc).where(
                OrganizationKyc.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if kyc is None:
        # Legacy-but-approved organizations (created before Phase 7 or via
        # direct status seeding) are grandfathered as APPROVED so existing
        # deployments keep operating; new enforcement applies from Phase 7.
        org = await db.get(Organization, organization_id)
        grandfathered = org is not None and getattr(org, "status", None) == "approved"
        kyc = OrganizationKyc(
            organization_id=organization_id,
            status=KycStatus.APPROVED if grandfathered else KycStatus.DRAFT,
            history=[{
                "action": "auto_provisioned",
                "actor": "system",
                "note": "grandfathered platform approval" if grandfathered else "draft",
                "at": _now().isoformat(),
            }],
        )
        db.add(kyc)
        await db.commit()
        await db.refresh(kyc)
    return kyc


async def admin_approve_provisions_kyc(
    db: AsyncSession, organization_id: int, admin_id: int
) -> OrganizationKyc:
    """Hook for admin_service.approve_organization: flips a pending org's
    KYC to its application state (or provisions a grandfathered record)."""
    kyc = await get_or_create_kyc(db, organization_id)
    if kyc.status in (KycStatus.APPROVED, KycStatus.RESTRICTED,
                      KycStatus.SUSPENDED, KycStatus.BLOCKED):
        return kyc  # enforcement state managed by the KYC desk only
    kyc.status = KycStatus.APPROVED
    kyc.reviewed_by = admin_id
    kyc.reviewed_at = _now()
    kyc.review_decision = "approved"
    _append(kyc, "approved", f"admin#{admin_id}", "platform approval")
    await db.commit()
    await db.refresh(kyc)
    return kyc


def _append(kyc: OrganizationKyc, action: str, actor: str, note: str) -> None:
    history = list(kyc.history or [])
    history.append({"action": action, "actor": actor, "note": note,
                    "at": _now().isoformat()})
    kyc.history = history


# ── application + submit ────────────────────────────────────────────────────

async def save_application(
    db: AsyncSession, organization_id: int, data: dict
) -> OrganizationKyc:
    kyc = await get_or_create_kyc(db, organization_id)
    if kyc.status in (KycStatus.SUBMITTED, KycStatus.UNDER_REVIEW):
        raise ValueError(
            "An application is already under review. Withdraw it via support first."
        )
    if kyc.status in KycStatus.FULLY_BLOCKED:
        raise PermissionError("KYC is suspended/blocked; contact platform support")
    allowed = set(REQUIRED_APPLICATION_FIELDS) | {"operating_address"}
    for key, value in data.items():
        if key in allowed:
            setattr(kyc, key, value)
    if kyc.status in (KycStatus.DRAFT, KycStatus.REJECTED, KycStatus.CONTACT_VERIFIED):
        _append(kyc, "application_updated", "organization", "draft data saved")
    await db.commit()
    await db.refresh(kyc)
    return kyc


async def upload_document(
    db: AsyncSession,
    organization_id: int,
    document_type: str,
    document_number: str,
    stored_path: Optional[str],
    expires_at: Optional[datetime],
) -> OrganizationVerification:
    kyc = await get_or_create_kyc(db, organization_id)
    if kyc.status in (KycStatus.SUSPENDED, KycStatus.BLOCKED):
        raise PermissionError("KYC is suspended/blocked; contact platform support")
    doc = OrganizationVerification(
        organization_id=organization_id,
        document_type=document_type,
        document_url=stored_path or f"record://{document_type}",
        document_number=document_number,
        expires_at=expires_at,
        verification_status=VerificationStatus.PENDING,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    await log_audit_event(
        db, action="kyc_document_uploaded", actor_type="organization",
        actor_id=organization_id, organization_id=organization_id,
        target_type="organization_verification", target_id=doc.id,
        details={"document_type": document_type},
    )
    return doc


async def submit_application(
    db: AsyncSession, organization_id: int, admin_id: Optional[int] = None
) -> OrganizationKyc:
    """Submit for review; validates required data + document requirements.
    Raises ValueError listing missing items; org must be platform-approved
    first (org.status semantics preserved)."""
    kyc = await get_or_create_kyc(db, organization_id)
    if kyc.status not in (
        KycStatus.DRAFT, KycStatus.CONTACT_VERIFIED, KycStatus.REJECTED,
        KycStatus.MORE_INFO_REQUIRED,
    ):
        raise ValueError(f"Application cannot be submitted from state {kyc.status}")
    org = await db.get(Organization, organization_id)
    if org is None:
        raise LookupError("Organization not found")

    unresolved_fields = [f for f in REQUIRED_APPLICATION_FIELDS if not getattr(kyc, f)]

    requirements = await get_doc_requirements(
        db, kyc.org_category or org.org_type, organization_id
    )
    docs = (
        await db.execute(
            select(OrganizationVerification).where(
                OrganizationVerification.organization_id == organization_id
            )
        )
    ).scalars().all()
    present_types = {d.document_type for d in docs}
    missing_docs = sorted(requirements - present_types)

    snapshot_fields = {
        "legal_name": kyc.legal_name, "registration_number": kyc.registration_number,
        "org_category": kyc.org_category,
        "registered_address": kyc.registered_address,
        "contact_email": kyc.contact_email, "contact_phone": kyc.contact_phone,
        "representative_name": kyc.representative_name,
        "representative_role": kyc.representative_role,
        "representative_email": kyc.representative_email,
        "representative_phone": kyc.representative_phone,
    }  # operating_address stays optional
    truthy_fields = [f for f, v in snapshot_fields.items() if not v]
    missing_fields = sorted(set(unresolved_fields) | set(truthy_fields))

    problems = []
    if missing_fields:
        problems.append(f"missing fields: {', '.join(missing_fields)}")
    if missing_docs:
        problems.append(f"missing documents: {', '.join(missing_docs)}")
    if kyc.status == KycStatus.MORE_INFO_REQUIRED:
        raise ValueError(
            "Resolve the more-information request before resubmitting: "
            f"{kyc.more_info_request or ''}"
        )
    if problems:
        raise ValueError("Application incomplete — " + "; ".join(problems))

    if kyc.status == KycStatus.DRAFT:
        kyc.status = KycStatus.CONTACT_VERIFIED
    kyc.status = KycStatus.SUBMITTED
    kyc.rejection_reason = None
    kyc.review_decision = None
    _append(kyc, "submitted", "organization", "application submitted for review")
    await db.commit()
    await db.refresh(kyc)
    await log_audit_event(
        db, action="kyc_submitted", actor_type="organization",
        actor_id=organization_id, organization_id=organization_id,
        target_type="organization_kyc", target_id=kyc.id,
        details={"documents": len(docs)},
    )
    return kyc


def _is_expired(kyc: OrganizationKyc) -> bool:
    return (
        kyc.status == KycStatus.APPROVED
        and _expires_in_past(kyc.expires_at)
    )


def _effective_status(kyc: OrganizationKyc) -> str:
    """An expired approval behaves like suspended until reviewed."""
    return KycStatus.SUSPENDED if _is_expired(kyc) else kyc.status


# ── ORGKYC-005 enforcement ─────────────────────────────────────────────────

async def assert_booking_allowed(
    db: AsyncSession,
    organization_id: int,
    paid_service: bool = False,
) -> None:
    """Raises PermissionError-free ValueError (booking-path convention)
    when the organization's trust state blocks public tokens/payments."""
    kyc = (
        await db.execute(
            select(OrganizationKyc).where(
                OrganizationKyc.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if kyc is None:
        return  # grandfathered legacy org — pre-Phase 7 behaviour preserved

    effective = _effective_status(kyc)
    if effective in KycStatus.FULLY_BLOCKED:
        raise ValueError(
            "This organization's verification is currently "
            f"{effective}; public booking is unavailable"
        )
    if paid_service and effective in KycStatus.PAID_ONLY_BLOCKED:
        raise ValueError(
            "Paid services are unavailable while this organization's "
            f"KYC is {effective}. Please complete organization verification."
        )


# ── review desk (ORGKYC-006) ───────────────────────────────────────────────

async def review_queue(db: AsyncSession) -> list[dict]:
    """Admin queue with computed missing/expired/duplicate indicators."""
    kycs = (
        await db.execute(
            select(OrganizationKyc)
            .where(OrganizationKyc.status.in_([
                KycStatus.SUBMITTED, KycStatus.UNDER_REVIEW,
                KycStatus.MORE_INFO_REQUIRED,
            ]))
            .order_by(OrganizationKyc.id)
        )
    ).scalars().all()
    out = []
    for kyc in kycs:
        org = await db.get(Organization, kyc.organization_id)
        indicators = await build_review_indicators(db, kyc)
        out.append({
            "organization_id": kyc.organization_id,
            "org_name": org.name if org else None,
            "status": kyc.status,
            "tier": kyc.tier,
            "expires_at": kyc.expires_at,
            "reviewed_at": kyc.reviewed_at,
            "appeal_status": kyc.appeal_status,
            "indicators": indicators,
        })
    return out


async def build_review_indicators(
    db: AsyncSession, kyc: OrganizationKyc
) -> dict:
    requirements = await get_doc_requirements(
        db, kyc.org_category, kyc.organization_id
    )
    docs = (
        await db.execute(
            select(OrganizationVerification).where(
                OrganizationVerification.organization_id == kyc.organization_id
            )
        )
    ).scalars().all()

    by_type: dict[str, int] = {}
    duplicate_doc_numbers = set()
    seen_numbers: set[str] = set()
    for d in docs:
        by_type[d.document_type] = by_type.get(d.document_type, 0) + 1
        if d.document_number:
            key = d.document_number.strip().lower()
            if key in seen_numbers:
                duplicate_doc_numbers.add(d.document_number)
            seen_numbers.add(key)

    return {
        "missing_documents": sorted(requirements - set(by_type)),
        "expired_documents": [
            d.id for d in docs if _expires_in_past(d.expires_at)
        ],
        "duplicate_document_numbers": sorted(duplicate_doc_numbers),
        "representative_verified": kyc.representative_verified,
        "grandfathered": kyc.review_decision is None and kyc.status == KycStatus.APPROVED,
        "total_documents": len(docs),
    }


async def kyc_review(
    db: AsyncSession,
    organization_id: int,
    admin_id: int,
    action: str,
    reason: Optional[str] = None,
    more_info_request: Optional[str] = None,
    tier: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    representative_verified: Optional[bool] = None,
) -> OrganizationKyc:
    kyc = await get_or_create_kyc(db, organization_id)
    action_map = {
        "under_review": KycStatus.UNDER_REVIEW,
        "more_info": KycStatus.MORE_INFO_REQUIRED,
        "approve": KycStatus.APPROVED,
        "reject": KycStatus.REJECTED,
        "restrict": KycStatus.RESTRICTED,
        "suspend": KycStatus.SUSPENDED,
        "block": KycStatus.BLOCKED,
        "unblock": KycStatus.APPROVED,
    }
    target = action_map[action]
    # Unblock is the explicit lift of SUSPENDED/BLOCKED/RESTRICTED.
    if action == "unblock":
        if kyc.status not in (KycStatus.SUSPENDED, KycStatus.BLOCKED, KycStatus.RESTRICTED):
            raise ValueError("unblock only applies to blocked/suspended states")
        kyc.status = KycStatus.APPROVED
    else:
        allowed = KycStatus.TRANSITIONS.get(kyc.status, set())
        if target not in allowed:
            raise ValueError(
                f"Illegal KYC transition {kyc.status} → {target}"
            )
        kyc.status = target

    if tier:
        kyc.tier = tier
    if action == "approve":
        kyc.expires_at = expires_at  # may be None = never expires
        kyc.rejection_reason = None
    if action == "reject":
        kyc.rejection_reason = reason or "Not specified"
    if action == "more_info":
        kyc.more_info_request = more_info_request or reason or "More information required"
    if representative_verified is not None:
        kyc.representative_verified = representative_verified
    kyc.reviewed_by = admin_id
    kyc.reviewed_at = _now()
    kyc.review_decision = action
    _append(kyc, action, f"admin#{admin_id}", reason or "")
    await db.commit()

    await log_audit_event(
        db, action=f"kyc_{action}", actor_type="admin", actor_id=admin_id,
        organization_id=organization_id,
        target_type="organization_kyc", target_id=kyc.id,
        details={"reason": reason or ""}, commit=True,
    )
    await db.refresh(kyc)
    return kyc


# ── appeals (ORGKYC-001) ───────────────────────────────────────────────────

APPEALABLE = {KycStatus.REJECTED, KycStatus.RESTRICTED, KycStatus.SUSPENDED}


async def file_appeal(
    db: AsyncSession, organization_id: int, reason: str
) -> OrganizationKyc:
    kyc = await get_or_create_kyc(db, organization_id)
    if _effective_status(kyc) not in APPEALABLE and kyc.appeal_status != "pending":
        raise ValueError("This KYC state is not appealable")
    if kyc.appeal_status == "pending":
        raise ValueError("An appeal is already pending")
    kyc.appeal_status = "pending"
    kyc.appeal_reason = reason
    kyc.appeal_decided_at = None
    kyc.appeal_decision_note = None
    _append(kyc, "appeal_filed", "organization", reason[:200])
    await db.commit()
    await log_audit_event(
        db, action="kyc_appeal_filed", actor_type="organization",
        actor_id=organization_id, organization_id=organization_id,
        target_type="organization_kyc", target_id=kyc.id,
    )
    await db.refresh(kyc)
    return kyc


async def decide_appeal(
    db: AsyncSession, organization_id: int, admin_id: int,
    decision: str, note: Optional[str] = None,
) -> OrganizationKyc:
    kyc = await get_or_create_kyc(db, organization_id)
    if kyc.appeal_status != "pending":
        raise ValueError("No pending appeal")
    kyc.appeal_status = decision
    kyc.appeal_decided_at = _now()
    kyc.appeal_decision_note = note
    if decision == "approved":
        if kyc.status == KycStatus.BLOCKED:
            raise ValueError("Blocked organizations cannot be reinstated by appeal")
        kyc.status = KycStatus.APPROVED
        kyc.rejection_reason = None
    _append(kyc, f"appeal_{decision}", f"admin#{admin_id}", note or "")
    await db.commit()
    await log_audit_event(
        db, action=f"kyc_appeal_{decision}", actor_type="admin",
        actor_id=admin_id, organization_id=organization_id,
        target_type="organization_kyc", target_id=kyc.id,
        details={"note": note or ""},
    )
    await db.refresh(kyc)
    return kyc


# ── branch-level verification (ORGKYC-004) ────────────────────────────────

async def verify_branch(
    db: AsyncSession, branch_id: int, admin_id: int, verified: bool = True
) -> Branch:
    branch = await db.get(Branch, branch_id)
    if branch is None:
        raise LookupError("Branch not found")
    branch.kyc_verified = verified
    branch.kyc_verified_at = _now() if verified else None
    branch.kyc_verified_by = admin_id if verified else None
    await db.commit()
    await log_audit_event(
        db,
        action="branch_kyc_verified" if verified else "branch_kyc_revoked",
        actor_type="admin", actor_id=admin_id,
        organization_id=branch.organization_id,
        target_type="branch", target_id=branch.id,
    )
    await db.refresh(branch)
    return branch


# ── private document access + retention (ORGKYC-007) ──────────────────────

async def list_documents(
    db: AsyncSession, organization_id: int
) -> list[dict]:
    docs = (
        await db.execute(
            select(OrganizationVerification)
            .where(OrganizationVerification.organization_id == organization_id)
            .order_by(OrganizationVerification.id)
        )
    ).scalars().all()
    return [
        {
            "id": d.id,
            "document_type": d.document_type,
            "document_number_masked": mask_document_number(d.document_number),
            "verification_status": d.verification_status.value
            if hasattr(d.verification_status, "value") else d.verification_status,
            "expires_at": d.expires_at,
            "expired": _expires_in_past(d.expires_at),
        }
        for d in docs
    ]


async def access_document(
    db: AsyncSession, organization_id: int, document_id: int,
    actor_type: str, actor_id: Optional[int],
) -> OrganizationVerification:
    """Ownership/reviewer-gated fetch with mandatory audit (masked
    document numbers are exposed in listings; the file path never is)."""
    doc = await db.get(OrganizationVerification, document_id)
    if doc is None or doc.organization_id != organization_id:
        raise LookupError("Document not found")
    await log_audit_event(
        db, action="kyc_document_accessed", actor_type=actor_type,
        actor_id=actor_id, organization_id=organization_id,
        target_type="organization_verification", target_id=doc.id,
    )
    return doc


async def purge_expired_documents(
    db: AsyncSession, organization_id: int, admin_id: int
) -> int:
    """ORGKYC-007 retention control: clears expired document file
    references (rows keep masked audit history)."""
    now = _now()
    docs = (
        await db.execute(
            select(OrganizationVerification).where(
                OrganizationVerification.organization_id == organization_id,
                OrganizationVerification.expires_at.is_not(None),
                OrganizationVerification.expires_at <= now,
            )
        )
    ).scalars().all()
    count = 0
    for doc in docs:
        # Tombstone the file reference (column is NOT NULL); masked audit
        # history stays in document_number.
        doc.document_url = f"purged://{doc.id}"
        count += 1
    if count:
        await log_audit_event(
            db, action="kyc_documents_purged", actor_type="admin",
            actor_id=admin_id, organization_id=organization_id,
            target_type="organization_kyc", target_id=organization_id,
            details={"count": count},
        )
        await db.commit()
    return count
