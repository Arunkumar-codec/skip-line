"""
models/organization.py
───────────────────────
Organization accounts (hospitals, banks, temples, etc.).
Registration creates a pending record; admin approval enables login.
"""

import enum

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import DateTime

from app.database import Base
from models.mixins import TimestampMixin


class OrgStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


class OrgType(str, enum.Enum):
    HOSPITAL = "hospital"
    CLINIC = "clinic"
    BANK = "bank"
    GOVERNMENT = "government"
    TEMPLE = "temple"
    EDUCATIONAL = "educational"
    SERVICE_CENTER = "service_center"
    OTHER = "other"


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    registration_number: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    org_type: Mapped[str] = mapped_column(
        Enum(OrgType), nullable=False, default=OrgType.OTHER
    )
    address: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    mobile: Mapped[str] = mapped_column(String(20), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(OrgStatus), nullable=False, default=OrgStatus.PENDING, index=True
    )
    logo_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Admin who approved/rejected this org
    approved_by_admin_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    approved_by: Mapped["Admin | None"] = relationship(
        "Admin", back_populates="approved_organizations", foreign_keys=[approved_by_admin_id]
    )
    verifications: Mapped[list["OrganizationVerification"]] = relationship(
        "OrganizationVerification", back_populates="organization", cascade="all, delete-orphan"
    )
    services: Mapped[list["Service"]] = relationship(
        "Service", back_populates="organization", cascade="all, delete-orphan"
    )
    counters: Mapped[list["Counter"]] = relationship(
        "Counter", back_populates="organization", cascade="all, delete-orphan"
    )
    tokens: Mapped[list["Token"]] = relationship(
        "Token", back_populates="organization", cascade="all, delete-orphan"
    )
    payments: Mapped[list["Payment"]] = relationship(
        "Payment", back_populates="organization"
    )
    queue_logs: Mapped[list["QueueLog"]] = relationship(
        "QueueLog", back_populates="organization", cascade="all, delete-orphan"
    )
    settings: Mapped[list["Setting"]] = relationship(
        "Setting", back_populates="organization", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Organization id={self.id} name={self.name!r} status={self.status}>"


# ── Staff roles ───────────────────────────────────────────────────────────────

import enum as _enum


class StaffRole(str, _enum.Enum):
    OWNER = "owner"
    COUNTER_EMPLOYEE = "counter_employee"
    KIOSK = "kiosk"


class OrgStaff(Base, TimestampMixin):
    """Staff members attached to an organization (counter employees, kiosk operators, owners)."""
    __tablename__ = "org_staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    mobile: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        Enum(StaffRole), nullable=False, default=StaffRole.COUNTER_EMPLOYEE
    )
    counter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    organization: Mapped["Organization"] = relationship(
        "Organization", foreign_keys=[organization_id]
    )

    def __repr__(self) -> str:
        return f"<OrgStaff id={self.id} email={self.email!r} role={self.role}>"


# ── Monthly schedule & holiday management ─────────────────────────────────────

class OrgSchedule(Base, TimestampMixin):
    """Override opening hours / capacity for a specific date."""
    __tablename__ = "org_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schedule_date: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    opening_time: Mapped[str | None] = mapped_column(String(10), nullable=True)
    closing_time: Mapped[str | None] = mapped_column(String(10), nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship(
        "Organization", foreign_keys=[organization_id]
    )


class OrgHoliday(Base, TimestampMixin):
    """Holiday / closure dates."""
    __tablename__ = "org_holidays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    holiday_date: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    organization: Mapped["Organization"] = relationship(
        "Organization", foreign_keys=[organization_id]
    )
