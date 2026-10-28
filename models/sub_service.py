"""
models/sub_service.py
─────────────────────
Sub-services under parent services.
Provides hierarchical breakdown, distinct pricing, additional estimated duration,
and service codes.
"""

from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class SubService(Base, TimestampMixin):
    __tablename__ = "sub_services"
    __table_args__ = (
        UniqueConstraint("service_id", "code", name="uq_service_subservice_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Additional pricing added to parent service base price
    additional_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    # Additional minutes added to parent service estimated duration
    additional_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    service: Mapped["Service"] = relationship("Service", back_populates="sub_services")

    def __repr__(self) -> str:
        return f"<SubService id={self.id} service_id={self.service_id} code={self.code!r} name={self.name!r}>"
