"""
models/user_identity.py
───────────────────────
Customer Identity Verification (KYC) records.
Stores customer government identity documents, encrypted ID numbers,
masked representations for displays (e.g. ••••1234), review status,
and rejection reasons.
"""

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class UserIdentity(Base, TimestampMixin):
    __tablename__ = "user_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    id_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # national_id, passport, driving_license, aadhaar, voters_id, other
    encrypted_id_number: Mapped[str] = mapped_column(Text, nullable=False)
    masked_id_number: Mapped[str] = mapped_column(String(50), nullable=False)
    document_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending", index=True
    )  # pending, approved, rejected
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    reviewed_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="identities")
    organization: Mapped["Organization | None"] = relationship("Organization")
    reviewer: Mapped["Employee | None"] = relationship("Employee")

    def __repr__(self) -> str:
        return f"<UserIdentity id={self.id} user={self.user_id} type={self.id_type!r} status={self.status}>"
