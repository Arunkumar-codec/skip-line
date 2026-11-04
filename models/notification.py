"""
models/notification.py (extended in Phase 8)
────────────────────────────────────────────
In-app notifications. Phase 8 additions (NTF-002):
  - `delivery_state`: for in-app rows the record IS the delivery, so rows
    default to `delivered`; the value distinguishes any transport-level
    record from legacy rows.
  - `template_version`: template identifier used to render the copy.
  - `dedup_key`: supports the NTF-005 queue-call duplicate-suppression
    window (e.g. re-calls inside the window reuse the existing row).

`NotificationPreference` (NTF-004) lets users disable OPTIONAL types;
any type in ESSENTIAL_TYPES can never be disabled (security, payment and
service-disruption notices).

The `notifications` table columns are guarded plain Strings / plain ADD
COLUMNs — no MySQL ENUM ALTERs (standing Phase-7 decision).
"""

import enum

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class NotificationChannel(str, enum.Enum):
    EMAIL = "email"
    SMS = "sms"
    IN_APP = "in_app"


class NotificationType(str, enum.Enum):
    TOKEN_CREATED = "token_created"
    QUEUE_NEARING = "queue_nearing"
    TOKEN_CALLED = "token_called"
    TOKEN_COMPLETED = "token_completed"
    TOKEN_CANCELLED = "token_cancelled"
    ORG_APPROVED = "org_approved"
    ORG_REJECTED = "org_rejected"
    PAYMENT_SUCCESS = "payment_success"
    # SCH-007 / Decision 11: schedule changes affecting a booking.
    # The production `notifications.type` column is a plain VARCHAR(40)
    # (migration 0001), so this member needs no ALTER TABLE; it is stored
    # as the enum member name exactly like every other notification type.
    SCHEDULE_CHANGE = "schedule_change"
    # Phase 8 (NTF-003 minimal-risk set): enforcement + security events.
    ENFORCEMENT_ACTION = "enforcement_action"
    SECURITY_NOTICE = "security_notice"


# NTF-004: security, payment and service-disruption notices cannot be
# disabled by user preference.
ESSENTIAL_TYPES = frozenset({
    NotificationType.PAYMENT_SUCCESS.value,
    NotificationType.SCHEDULE_CHANGE.value,
    NotificationType.ENFORCEMENT_ACTION.value,
    NotificationType.SECURITY_NOTICE.value,
})


class DeliveryState:
    """NTF-002 delivery states for the (in-app) notifications table."""
    DELIVERED = "delivered"
    ALL = (DELIVERED,)


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[str] = mapped_column(Enum(NotificationType), nullable=False)
    channel: Mapped[str] = mapped_column(Enum(NotificationChannel), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sent_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Phase 8 (NTF-002) ────────────────────────────────────────────────────
    delivery_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DeliveryState.DELIVERED,
        server_default=DeliveryState.DELIVERED,
    )
    template_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="notifications")
    token: Mapped["Token | None"] = relationship("Token", back_populates="notifications")

    def __repr__(self) -> str:
        return f"<Notification id={self.id} type={self.type} user={self.user_id}>"


class NotificationPreference(Base, TimestampMixin):
    """NTF-004 — per-user delivery preferences for OPTIONAL notification
    types. Essential types (ESSENTIAL_TYPES) are rejected by the service
    layer and can never be silenced."""
    __tablename__ = "notification_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    # JSON list of NotificationType values the user opted OUT of.
    disabled_types: Mapped[list | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<NotificationPreference user={self.user_id} disabled={self.disabled_types}>"
