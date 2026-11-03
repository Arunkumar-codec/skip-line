"""
models/organization_kyc.py
─────────────────────────
Phase 7 — Organization-level KYC (ORGKYC-001..007). This is the
ORGANIZATION trust workflow (distinct from the Phase 2 customer identity
desk in models/user_identity.py).

The tiered status taxonomy (ORGKYC-001) lives here as plain String
values (like notifications.type) so no MySQL ENUM ALTER is ever needed:

    draft → contact_verified → submitted → under_review
          → more_info_required → submitted (resubmit)
          → approved | rejected | restricted | suspended | blocked

plus expires_at (academic/limited approvals) and the appeal fields.
Review history is appended into `history` JSON as an audit-safe record
alongside audit_logs events.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class KycStatus:
    DRAFT = "draft"
    CONTACT_VERIFIED = "contact_verified"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    MORE_INFO_REQUIRED = "more_info_required"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESTRICTED = "restricted"
    SUSPENDED = "suspended"
    BLOCKED = "blocked"

    TAXONOMY = {
        DRAFT, CONTACT_VERIFIED, SUBMITTED, UNDER_REVIEW, MORE_INFO_REQUIRED,
        APPROVED, REJECTED, RESTRICTED, SUSPENDED, BLOCKED,
    }

    # Legal transitions (validated server-side; no transition bypass).
    TRANSITIONS = {
        DRAFT: {CONTACT_VERIFIED, SUBMITTED, BLOCKED},
        CONTACT_VERIFIED: {SUBMITTED, BLOCKED},
        SUBMITTED: {UNDER_REVIEW, APPROVED, REJECTED, MORE_INFO_REQUIRED, BLOCKED},
        UNDER_REVIEW: {APPROVED, REJECTED, MORE_INFO_REQUIRED, SUSPENDED, BLOCKED},
        MORE_INFO_REQUIRED: {SUBMITTED, REJECTED, BLOCKED},   # resubmit / give up
        # APPROVED: withdrawal of grandfathered approval back to REJECTED
        # (forces a fresh KYC application), tightened further down, or final.
        APPROVED: {RESTRICTED, SUSPENDED, BLOCKED, REJECTED},
        REJECTED: {SUBMITTED, DRAFT},                         # fresh application
        RESTRICTED: {APPROVED, SUSPENDED, BLOCKED},           # lifted or worse
        SUSPENDED: {APPROVED, RESTRICTED, BLOCKED},
        BLOCKED: set(),
    }

    # States that block all public booking/payment, and states that only
    # block PAID services (ORGKYC-005).
    FULLY_BLOCKED = {REJECTED, SUSPENDED, BLOCKED}
    PAID_ONLY_BLOCKED = {RESTRICTED, MORE_INFO_REQUIRED}


class OrganizationKyc(Base, TimestampMixin):
    __tablename__ = "organization_kyc"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=KycStatus.DRAFT, index=True)
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="base")

    # ORGKYC-002 application payload (org profile snapshot for review).
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    registration_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    org_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    registered_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    operating_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    representative_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    representative_role: Mapped[str | None] = mapped_column(String(100), nullable=True)
    representative_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    representative_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    representative_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # ORGKYC-006 review desk fields.
    reviewed_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped["datetime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    review_decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    more_info_request: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ORGKYC-001 tiered expiry: APPROved tiers may carry an expiry date;
    # an expired approval behaves like suspended until renewed/reviewed.
    expires_at: Mapped["datetime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    # Appeal fields (ORGKYC-001): appeal against REJECTED/RESTRICTED/SUSPENDED.
    appeal_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # pending|approved|denied
    appeal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_decided_at: Mapped["datetime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    appeal_decision_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Append-only history: [{action, actor, note, at}] — lets the review UI
    # show full approvals/rejections history (ORGKYC-006).
    history: Mapped[list | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<OrganizationKyc org={self.organization_id} status={self.status}>"
