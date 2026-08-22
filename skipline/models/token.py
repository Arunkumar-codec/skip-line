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

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


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

    # Payment snapshot (denormalised for fast receipt generation)
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )

    # Lifecycle timestamps
    booked_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    called_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    def __repr__(self) -> str:
        return f"<Token id={self.id} number={self.token_number!r} status={self.status}>"


class TokenType(str, enum.Enum):
    """How the token was obtained."""
    ONLINE   = "online"    # booked via web/app by a registered user
    PHYSICAL = "physical"  # issued at the counter for a walk-in guest
    KIOSK    = "kiosk"     # booked from a self-service kiosk terminal
    FUTURE   = "future"    # advance / scheduled booking
