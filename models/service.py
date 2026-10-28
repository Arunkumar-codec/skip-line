"""
models/service.py
─────────────────
Services offered by an organization (e.g. General Consultation, Billing).
Each service has an optional price and estimated duration used for wait-time
calculations.
"""

from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class Service(Base, TimestampMixin):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    branch_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("branches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    service_code: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Price = 0 means free; NULL means price not set (pay at counter)
    price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    requires_identity_verification: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="services"
    )
    branch: Mapped["Branch | None"] = relationship(
        "Branch"
    )
    counters: Mapped[list["Counter"]] = relationship(
        "Counter", back_populates="service"
    )
    counter_services: Mapped[list["CounterService"]] = relationship(
        "CounterService", back_populates="service", cascade="all, delete-orphan"
    )
    sub_services: Mapped[list["SubService"]] = relationship(
        "SubService", back_populates="service", cascade="all, delete-orphan"
    )
    custom_forms: Mapped[list["CustomForm"]] = relationship(
        "CustomForm", back_populates="service"
    )
    tokens: Mapped[list["Token"]] = relationship(
        "Token", back_populates="service"
    )

    def __repr__(self) -> str:
        return f"<Service id={self.id} code={self.service_code!r} name={self.name!r} org={self.organization_id}>"
