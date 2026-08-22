"""
models/counter.py
─────────────────
Physical service counters within an organization.
Each counter is associated with one service and uses a token prefix
(e.g. "A") to generate tokens like A-001, A-002.
"""

import enum

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class CounterStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    BREAK = "break"


class Counter(Base, TimestampMixin):
    __tablename__ = "counters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )
    counter_name: Mapped[str] = mapped_column(String(100), nullable=False)
    counter_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Prefix for tokens, e.g. "A" → A-001
    token_prefix: Mapped[str] = mapped_column(String(5), nullable=False, default="A")
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    status: Mapped[str] = mapped_column(
        Enum(CounterStatus), nullable=False, default=CounterStatus.ACTIVE
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="counters"
    )
    service: Mapped["Service | None"] = relationship(
        "Service", back_populates="counters"
    )
    tokens: Mapped[list["Token"]] = relationship(
        "Token", back_populates="counter"
    )
    queue_logs: Mapped[list["QueueLog"]] = relationship(
        "QueueLog", back_populates="counter"
    )

    def __repr__(self) -> str:
        return f"<Counter id={self.id} name={self.counter_name!r} prefix={self.token_prefix!r}>"
