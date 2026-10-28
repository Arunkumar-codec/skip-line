"""
models/token_service_item.py
────────────────────────────
Records the individual services and sub-services requested on a token.
Allows multi-service token packages and detailed service history tracking.
"""

from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class TokenServiceItem(Base, TimestampMixin):
    __tablename__ = "token_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sub_service_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sub_services.id", ondelete="SET NULL"), nullable=True, index=True
    )
    price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sequence_order: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Relationships
    token: Mapped["Token"] = relationship("Token", back_populates="token_services")
    service: Mapped["Service"] = relationship("Service")
    sub_service: Mapped["SubService | None"] = relationship("SubService")

    def __repr__(self) -> str:
        return f"<TokenServiceItem token={self.token_id} service={self.service_id} sub={self.sub_service_id}>"
