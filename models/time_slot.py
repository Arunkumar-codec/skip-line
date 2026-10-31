"""
models/time_slot.py
───────────────────
Atomic appointment slots for counters running in slot mode (Decision 10).

Capacity accounting is layered and conceptually separate (Decisions 10/13):
- `capacity`      — maximum confirmable bookings for the slot
- `booked_count`  — confirmed bookings
- `held_count`    — capacity reserved by pending-payment reservations
                    (Phase 5 integration contract, Decision 14: holds
                    carry `expires_at` at the reservation level and must
                    be idempotent to release; this column only tracks
                    how much capacity is currently held)

The atomic reservation itself is a guarded `UPDATE
… SET booked_count = booked_count + 1 WHERE id = :id AND
booked_count + held_count < capacity` performed by the booking engine
(portable across MySQL and SQLite; see OPEN_DECISIONS §4).
"""

import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class TimeSlot(Base, TimestampMixin):
    __tablename__ = "time_slots"
    __table_args__ = (
        UniqueConstraint(
            "counter_id", "slot_date", "start_time",
            name="uq_time_slot_counter_date_start",
        ),
        CheckConstraint(
            "capacity >= 0",
            name="ck_time_slot_capacity_nonnegative",
        ),
        CheckConstraint(
            "booked_count >= 0",
            name="ck_time_slot_booked_nonnegative",
        ),
        CheckConstraint(
            "held_count >= 0",
            name="ck_time_slot_held_nonnegative",
        ),
        CheckConstraint(
            "booked_count + held_count <= capacity",
            name="ck_time_slot_within_capacity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    counter_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="SET NULL"), nullable=True, index=True
    )

    slot_date: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    start_time: Mapped[datetime.time] = mapped_column(Time(), nullable=False)
    end_time: Mapped[datetime.time] = mapped_column(Time(), nullable=False)

    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    booked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    held_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships (one-directional — no back_populates on Counter/Service)
    organization: Mapped["Organization"] = relationship("Organization")
    counter: Mapped["Counter"] = relationship("Counter")
    service: Mapped["Service | None"] = relationship("Service")
    tokens: Mapped[list["Token"]] = relationship(
        "Token",
        primaryjoin="Token.slot_id == TimeSlot.id",
        foreign_keys="Token.slot_id",
        viewonly=True,
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return (
            f"<TimeSlot id={self.id} counter={self.counter_id} "
            f"date={self.slot_date} {self.start_time}-{self.end_time} "
            f"booked={self.booked_count}/{self.capacity}>"
        )
