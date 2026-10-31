"""
models/notification.py
───────────────────────
Notifications sent to users for token lifecycle events.
Channels: email | sms | in_app
"""

import enum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


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


class Notification(Base):
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

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="notifications")
    token: Mapped["Token | None"] = relationship("Token", back_populates="notifications")

    def __repr__(self) -> str:
        return f"<Notification id={self.id} type={self.type} user={self.user_id}>"
