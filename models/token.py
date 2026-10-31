"""
models/token.py
───────────────
Virtual queue token — the central entity of Skip Line.

Token number format: {prefix}-{sequence:03d}  →  A-001, B-042, C-200
Status lifecycle:
    waiting → called → in_progress → completed
                    ↘ skipped
                    ↘ cancelled
"""

import enum
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.capacity_rule import BookingSource
from models.mixins import TimestampMixin
from models.weekly_schedule import _enum_values


class TokenStatus(str, enum.Enum):
    WAITING = "waiting"
    CALLED = "called"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    ON_HOLD = "on_hold"


class Token(Base, TimestampMixin):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    counter_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    service_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )

    # Appointment slot for slot-mode bookings (Decision 10).
    # NULL for day-queue tokens (all legacy/Phase 1-2 tokens stay valid).
    slot_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("time_slots.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Issuing channel (SCH-004 source-based allocation; CTR-003). Set
    # server-side by each booking route — never trusted from client
    # input. Legacy rows default to walk_in counters from Phase 1-2.
    booking_source: Mapped[str] = mapped_column(
        Enum(BookingSource, values_callable=_enum_values),
        nullable=False,
        default=BookingSource.WALK_IN,
        server_default=BookingSource.WALK_IN.value,
    )

    # Display identifier, e.g. "A-023"
    token_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Numeric sequence within the counter (used for queue ordering)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(
        Enum(TokenStatus), nullable=False, default=TokenStatus.WAITING, index=True
    )

    # Position 1 = next to be served; recalculated on each state change
    queue_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Guest booking fields (kiosk — no user account)
    guest_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    guest_mobile: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Files
    qr_code_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    receipt_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Channel-supplied idempotency reference (KSK-005 / PHY-001): a retried
    # kiosk or walk-in booking with the same reference returns the SAME
    # token instead of issuing a duplicate after a printer/network failure.
    client_reference: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Payment snapshot (denormalised for fast receipt generation)
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )

    # Lifecycle timestamps
    booked_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    called_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # FIN-001: Admin-initiated pause of a PAID booking. Pausing flips the
    # token to the existing TokenStatus.ON_HOLD (queue/display semantics
    # reused; capacity stays consumed per Open Decision 16) while keeping
    # the operator's previous status so resume restores it exactly.
    # Only tokens with amount_paid > 0 in WAITING/CALLED/IN_PROGRESS can
    # be paused; while this flag is set, operators cannot call/progress/
    # complete the token from any endpoint.
    payment_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    payment_hold_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payment_hold_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_hold_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_hold_previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Relationships
    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="tokens"
    )
    user: Mapped["User | None"] = relationship(
        "User", back_populates="tokens"
    )
    counter: Mapped["Counter"] = relationship(
        "Counter", back_populates="tokens"
    )
    service: Mapped["Service | None"] = relationship(
        "Service", back_populates="tokens"
    )
    slot: Mapped["TimeSlot | None"] = relationship(
        "TimeSlot", foreign_keys=[slot_id]
    )
    history: Mapped[list["TokenHistory"]] = relationship(
        "TokenHistory", back_populates="token", cascade="all, delete-orphan",
        order_by="TokenHistory.changed_at",
    )
    payments: Mapped[list["Payment"]] = relationship(
        "Payment", back_populates="token"
    )
    notifications: Mapped[list["Notification"]] = relationship(
        "Notification", back_populates="token", cascade="all, delete-orphan"
    )
    queue_logs: Mapped[list["QueueLog"]] = relationship(
        "QueueLog", back_populates="token"
    )
    form_snapshot: Mapped["TokenFormSnapshot | None"] = relationship(
        "TokenFormSnapshot", back_populates="token", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    token_services: Mapped[list["TokenServiceItem"]] = relationship(
        "TokenServiceItem", back_populates="token", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Token id={self.id} number={self.token_number!r} status={self.status}>"
