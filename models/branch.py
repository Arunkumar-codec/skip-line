"""
models/branch.py
────────────────
Physical branch / location of an organization (e.g. Main Hospital, City Clinic).
Used to scope physical counters, staff, and localized queue configurations.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class Branch(Base, TimestampMixin):
    __tablename__ = "branches"
    __table_args__ = (
        UniqueConstraint("organization_id", "branch_code", name="uq_org_branch_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    branch_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    pincode: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="branches")
    employees: Mapped[list["Employee"]] = relationship(
        "Employee", back_populates="branch", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Branch id={self.id} org_id={self.organization_id} code={self.branch_code!r} name={self.name!r}>"
