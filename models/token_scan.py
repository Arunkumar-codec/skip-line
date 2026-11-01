"""
models/token_scan.py
────────────────────
QR-005 scan audit trail: every authorized-scanner attempt (successful or
not) is recorded so an organization can prove who checked a token in, when,
on which device/counter, and what the scanner decided.

One row per scan attempt — duplicates of an already-checked-in token create
new audit rows but never re-mutate `tokens.checked_in_at`.
"""

import enum

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class ScanResult(str, enum.Enum):
    """QR-004 scan outcome vocabulary (incl. Marking SKIPPED — see OPEN_DECISIONS)."""

    VALID = "valid"                       # first successful check-in
    ALREADY_CHECKED_IN = "already_checked_in"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    EXPIRED = "expired"                   # signed ref past its expiry
    WRONG_BRANCH = "wrong_branch"
    WRONG_ORG = "wrong_org"               # ref belongs to a different tenant
    TAMPERED = "tampered"
    INVALID = "invalid"


class TokenScan(Base, TimestampMixin):
    __tablename__ = "token_scans"
    __table_args__ = (
        Index("ix_token_scans_org_token", "organization_id", "token_id"),
        Index("ix_token_scans_employee", "employee_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_id: Mapped[int | None] = mapped_column(
        # Nullable: scans rejected before token resolution (tampered/invalid
        # ref) are still audited under the scanner's organization.
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # The authenticated employee who scanned; NULL only for scanner attempts
    # rejected before authentication, which never reach this table.
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    counter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    result: Mapped[str] = mapped_column(
        Enum(ScanResult, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    device_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    scanned_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    token: Mapped["models.token.Token"] = relationship("Token")  # noqa: F821
    employee: Mapped["models.employee.Employee"] = relationship("Employee")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<TokenScan id={self.id} token={self.token_id} "
            f"employee={self.employee_id} result={self.result!r}>"
        )
