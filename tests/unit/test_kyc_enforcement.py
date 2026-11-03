"""
tests/unit/test_kyc_enforcement.py
──────────────────────────────────
Phase 7 — Organization KYC (ORGKYC-001..007) enforcement rules:

- Provisioning/grandfathering via get_or_create_kyc (legacy approved orgs
  preserved; new orgs start DRAFT).
- Configurable per-category document requirements (+ Setting override).
- Application completeness gate on submit.
- Taxonomy transitions are only legal per KycStatus.TRANSITIONS.
- Expiry ≡ suspended enforcement.
- ORGKYC-005 booking gate: blocked states stop booking; paid services
  additionally blocked for RESTRICTED / more_info_required.
- Appeals: appealable states, single pending appeal, block is final.
- Documents: masked numbers, ownership-only access, expiry purge.
- Branch-level verification toggling.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import kyc_service
from models.branch import Branch
from models.organization import Organization, OrgStatus
from models.organization_kyc import KycStatus, OrganizationKyc
from models.organization_verification import OrganizationVerification
from tests.phase7_helpers import make_branch, make_org

pytestmark = pytest.mark.asyncio


async def _approved_org(db_session: AsyncSession):
    return await make_org(db_session)


async def _new_org(db_session: AsyncSession):
    return await make_org(db_session, status=OrgStatus.PENDING)


# ── provisioning / grandfathering ────────────────────────────────────────────

async def test_grandfathered_and_fresh_provisioning(db_session: AsyncSession):
    approved = await _approved_org(db_session)
    fresh = await _new_org(db_session)

    kyc = await kyc_service.get_or_create_kyc(db_session, approved.id)
    assert kyc.status == KycStatus.APPROVED, "legacy approved org grandfathered"
    assert kyc.history and kyc.history[0]["action"] == "auto_provisioned"

    kyc2 = await kyc_service.get_or_create_kyc(db_session, fresh.id)
    assert kyc2.status == KycStatus.DRAFT
    assert kyc2.id != kyc.id

    # Idempotent: second call returns the same row.
    again = await kyc_service.get_or_create_kyc(db_session, approved.id)
    assert again.id == kyc.id


# ── configurable requirements (ORGKYC-003) ───────────────────────────────────

async def test_category_doc_requirements_and_override(db_session: AsyncSession):
    org = await _new_org(db_session)
    org.org_type = "hospital"
    await db_session.flush()

    requirements = await kyc_service.get_doc_requirements(
        db_session, org.org_type, org.id
    )
    assert {"registration_certificate", "medical_licence"} <= requirements

    # Per-org Setting override replaces the defaults.
    from models.setting import Setting

    db_session.add(Setting(
        organization_id=org.id, key="kyc_doc_requirements",
        value='["registration_certificate_only"]',
    ))
    await db_session.flush()
    override = await kyc_service.get_doc_requirements(
        db_session, org.org_type, org.id
    )
    assert override == {"registration_certificate_only"}

    # Unknown category falls back to defaults (fresh org without override).
    fallback_org = await _new_org(db_session)
    fallback = await kyc_service.get_doc_requirements(
        db_session, "martian", fallback_org.id
    )
    assert fallback == {"registration_certificate"}


# ── application lifecycle ────────────────────────────────────────────────────

FULL_APPLICATION = {
    "legal_name": "Trust Org Holdings",
    "registration_number": "REG-2026-0001",
    "org_category": "hospital",
    "registered_address": "1 Trust Street, Metro",
    "contact_email": "kyc@trustorg.example.com",
    "contact_phone": "9800000000",
    "representative_name": "Alice Representative",
    "representative_role": "Director",
    "representative_email": "alice@trustorg.example.com",
    "representative_phone": "9800000001",
}


async def test_submit_requires_complete_application_and_documents(
    db_session: AsyncSession,
):
    org = await _new_org(db_session)
    partial = dict(FULL_APPLICATION)
    del partial["representative_phone"]
    partial["operating_address"] = "2 Branch Road"
    await kyc_service.save_application(db_session, org.id, partial)

    with pytest.raises(ValueError, match="representative_phone"):
        await kyc_service.submit_application(db_session, org.id)

    await kyc_service.upload_document(
        db_session, org.id, "registration_certificate", "RC-123",
        None, None,
    )
    await kyc_service.upload_document(
        db_session, org.id, "medical_licence", "ML-456",
        None, None,
    )
    await kyc_service.save_application(
        db_session, org.id, FULL_APPLICATION | {"operating_address": "2 B Road"}
    )
    kyc = await kyc_service.submit_application(db_session, org.id)
    assert kyc.status == KycStatus.SUBMITTED

    # Re-submit while under review is blocked.
    with pytest.raises(ValueError, match="already under review|cannot be submitted"):
        await kyc_service.submit_application(db_session, org.id)


async def test_review_transitions_and_expiry(db_session: AsyncSession):
    org = await _new_org(db_session)
    await kyc_service.save_application(db_session, org.id, FULL_APPLICATION)
    await kyc_service.upload_document(
        db_session, org.id, "registration_certificate", "CERT-1", None, None
    )
    await kyc_service.upload_document(
        db_session, org.id, "medical_licence", "ML-456", None, None
    )
    kyc = await kyc_service.submit_application(db_session, org.id)

    # SUBMITTED → APPROVED directly is legal.
    kyc = await kyc_service.kyc_review(
        db_session, org.id, admin_id=7, action="approve",
        tier="standard", expires_at=datetime.now(timezone.utc) + timedelta(days=365),
        representative_verified=True,
    )
    assert kyc.status == KycStatus.APPROVED
    assert kyc.tier == "standard"
    assert kyc.representative_verified is True

    # APPROVED → REJECTED is legal: withdrawal of a (grandfathered)
    # approval, forcing the org into a fresh KYC application. The only way
    # back is the resubmission cycle (REJECTED → SUBMITTED → APPROVED).
    kyc = await kyc_service.kyc_review(
        db_session, org.id, 7, action="reject",
        reason="grandfathered approval withdrawn — reapply",
    )
    assert kyc.status == KycStatus.REJECTED
    with pytest.raises(ValueError, match="Illegal KYC transition"):
        await kyc_service.kyc_review(
            db_session, org.id, 7, action="approve", tier="standard",
        )

    # Back into the application cycle: REJECTED orgs may re-submit.
    kyc = await kyc_service.submit_application(db_session, org.id)
    assert kyc.status == KycStatus.SUBMITTED
    kyc = await kyc_service.kyc_review(
        db_session, org.id, 7, action="approve",
        tier="standard", expires_at=datetime.now(timezone.utc) + timedelta(days=365),
        representative_verified=True,
    )
    assert kyc.status == KycStatus.APPROVED

    # DRAFT → APPROVED illegal (fresh org flow).
    org2 = await _new_org(db_session)
    with pytest.raises(ValueError, match="Illegal KYC transition"):
        await kyc_service.kyc_review(db_session, org2.id, 7, action="approve")

    # Expiry: expired approval behaves like suspended.
    expired = await kyc_service.get_or_create_kyc(db_session, org.id)
    expired.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.flush()
    assert kyc_service._effective_status(expired) == KycStatus.SUSPENDED


async def test_review_queue_indicators(db_session: AsyncSession):
    org = await _new_org(db_session)
    await kyc_service.save_application(db_session, org.id, FULL_APPLICATION)
    await kyc_service.upload_document(
        db_session, org.id, "registration_certificate", "cert-99", None, None
    )
    # Duplicate document number across two rows.
    await kyc_service.upload_document(
        db_session, org.id, "medical_licence", "CERT-99", None, None
    )
    # Expired doc.
    await kyc_service.upload_document(
        db_session, org.id, "trade_licence", "TL-1", None,
        datetime.now(timezone.utc) - timedelta(days=1),
    )
    await kyc_service.submit_application(db_session, org.id)

    queue = await kyc_service.review_queue(db_session)
    entry = next(q for q in queue if q["organization_id"] == org.id)
    ind = entry["indicators"]
    assert ind["duplicate_document_numbers"] == ["CERT-99"]
    assert len(ind["expired_documents"]) == 1
    assert ind["representative_verified"] is False


async def test_booking_gate_enforcement(db_session: AsyncSession):
    """ORGKYC-005: KYC restrictions block tokens/payments server-side."""
    org = await _new_org(db_session)

    # No row yet → legacy behaviour untouched.
    await kyc_service.assert_booking_allowed(db_session, org.id)

    suspended = await kyc_service.get_or_create_kyc(db_session, org.id)
    suspended.status = KycStatus.SUSPENDED
    await db_session.flush()
    with pytest.raises(ValueError, match="suspended"):
        await kyc_service.assert_booking_allowed(db_session, org.id)

    suspended.status = KycStatus.APPROVED
    suspended.expires_at = datetime.now(timezone.utc) - timedelta(days=2)
    await db_session.flush()
    with pytest.raises(ValueError, match="suspended"):
        await kyc_service.assert_booking_allowed(db_session, org.id)  # expired

    restricted = await kyc_service.get_or_create_kyc(db_session, org.id)
    restricted.status = KycStatus.RESTRICTED
    restricted.expires_at = None
    await db_session.flush()
    # Free services still bookable; paid blocked.
    await kyc_service.assert_booking_allowed(db_session, org.id, paid_service=False)
    with pytest.raises(ValueError, match="Paid services"):
        await kyc_service.assert_booking_allowed(db_session, org.id, paid_service=True)

    more_info = await kyc_service.get_or_create_kyc(db_session, org.id)
    more_info.status = KycStatus.MORE_INFO_REQUIRED
    await db_session.flush()
    with pytest.raises(ValueError, match="Paid services"):
        await kyc_service.assert_booking_allowed(db_session, org.id, paid_service=True)

    restricted.status = KycStatus.BLOCKED
    await db_session.flush()
    with pytest.raises(ValueError, match="blocked"):
        await kyc_service.assert_booking_allowed(db_session, org.id)


async def test_appeals_lifecycle(db_session: AsyncSession):
    org = await _new_org(db_session)
    kyc = await kyc_service.get_or_create_kyc(db_session, org.id)

    # DRAFT is not appealable.
    with pytest.raises(ValueError, match="not appealable"):
        await kyc_service.file_appeal(db_session, org.id, "please reconsider!")

    kyc.status = KycStatus.REJECTED
    kyc.rejection_reason = "unclear documents"
    await db_session.flush()

    await kyc_service.file_appeal(db_session, org.id, "Documents are legitimate")
    assert kyc.appeal_status == "pending"
    with pytest.raises(ValueError, match="already pending"):
        await kyc_service.file_appeal(db_session, org.id, "again please!!")

    await kyc_service.decide_appeal(
        db_session, org.id, admin_id=3, decision="approved", note="ok"
    )
    assert kyc.appeal_status == "approved"
    assert kyc.status == KycStatus.APPROVED

    # Blocked orgs are terminal: no appeal may be filed at all.
    kyc.status = KycStatus.BLOCKED
    kyc.appeal_status = None
    await db_session.flush()
    with pytest.raises(ValueError, match="not appealable"):
        await kyc_service.file_appeal(db_session, org.id, "Please review the block")


async def test_document_masking_access_and_purge(db_session: AsyncSession):
    org = await _new_org(db_session)
    doc = await kyc_service.upload_document(
        db_session, org.id, "registration_certificate", "long-secret-9876",
        None, datetime.now(timezone.utc) - timedelta(days=30),
    )
    assert doc.document_number == "long-secret-9876"

    docs = await kyc_service.list_documents(db_session, org.id)
    listing = docs[0]
    assert listing["document_number_masked"].endswith("9876")
    assert "long-secret" not in listing["document_number_masked"]
    assert listing["expired"] is True

    # Ownership-scoped access (foreign org can't reach it).
    foreign = await _approved_org(db_session)
    with pytest.raises(LookupError):
        await kyc_service.access_document(
            db_session, foreign.id, doc.id, "admin", 7
        )
    await kyc_service.access_document(db_session, org.id, doc.id, "admin", 7)

    # Admin-only retention purge.
    purged = await kyc_service.purge_expired_documents(
        db_session, org.id, admin_id=7
    )
    assert purged == 1
    await db_session.refresh(doc)
    assert doc.document_url.startswith("purged://"), "file reference tombstoned"


async def test_branch_verification(db_session: AsyncSession):
    org = await _new_org(db_session)
    branch = await make_branch(db_session, org)
    assert branch.kyc_verified is False

    await kyc_service.verify_branch(db_session, branch.id, admin_id=5, verified=True)
    assert branch.kyc_verified is True
    assert branch.kyc_verified_by == 5
    assert branch.kyc_verified_at is not None

    await kyc_service.verify_branch(db_session, branch.id, admin_id=5, verified=False)
    assert branch.kyc_verified is False
    assert branch.kyc_verified_at is None
