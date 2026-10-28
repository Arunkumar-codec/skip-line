"""
models/counter_service.py
─────────────────────────
Junction table mapping Counters to Services (Many-to-Many).
Enables multi-service counters where a single counter can serve
multiple service types (e.g. Counter 1 handles Cash Deposits and Account Queries).
"""

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CounterService(Base):
    __tablename__ = "counter_services"
    __table_args__ = (
        UniqueConstraint("counter_id", "service_id", name="uq_counter_service"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    counter_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    counter: Mapped["Counter"] = relationship("Counter", back_populates="counter_services")
    service: Mapped["Service"] = relationship("Service", back_populates="counter_services")

    def __repr__(self) -> str:
        return f"<CounterService counter={self.counter_id} service={self.service_id} primary={self.is_primary}>"
