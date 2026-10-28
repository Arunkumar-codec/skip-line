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
    branches: Mapped[list["Branch"]] = relationship(
        "Branch", back_populates="organization", cascade="all, delete-orphan"
    )
    employees: Mapped[list["Employee"]] = relationship(
        "Employee", back_populates="organization", cascade="all, delete-orphan"
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
    custom_forms: Mapped[list["CustomForm"]] = relationship(
        "CustomForm", back_populates="organization", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Organization id={self.id} name={self.name!r} status={self.status}>"
