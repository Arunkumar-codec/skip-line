"""
models/organization_verification.py
────────────────────────────────────
Document verification records for organization registration.
An organization may upload multiple documents (registration cert, tax ID, etc.).
"""

import enum

from sqlalchemy import Enum, ForeignKey, Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class VerificationStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class OrganizationVerification(Base, TimestampMixin):
    __tablename__ = "organization_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_type: Mapped[str] = mapped_column(String(100), nullable=False)
    document_url: Mapped[str] = mapped_column(String(500), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        Enum(VerificationStatus), nullable=False, default=VerificationStatus.PENDING
    )
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="verifications"
    )

    def __repr__(self) -> str:
        return f"<OrgVerification id={self.id} org={self.organization_id} status={self.verification_status}>"
