"""
models/notification_outbox.py
─────────────────────────────
Phase 8 — Durable notification delivery (NTF-001/002/005).

`Notification` remains the user-visible in-app record. The outbox is the
durable delivery queue for *outbound* channels (email / SMS):

  - every queued delivery stores recipient, type, template version,
    related entity, channel, delivery state, attempt count and timestamps
    (NTF-002);
  - failed sends are retried with exponential backoff up to
    `max_attempts` and are NEVER marked sent (delivery-state correctness);
  - `dedup_key` supports the queue-call duplicate-suppression window
    (NTF-005);
  - guest recipients (kiosk tokens without a user account) are supported:
    user_id is NULL and the mobile/email is carried directly.

String status columns follow the standing guarded-String decision
(no MySQL ENUM ALTERs).
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from models.mixins import TimestampMixin


class OutboxStatus:
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"  # deduplicated / dropped by preference policy

    ALL = (PENDING, SENDING, SENT, FAILED, SUPPRESSED)


class NotificationOutbox(Base, TimestampMixin):
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    channel: Mapped[str] = mapped_column(String(20), nullable=False)   # email | sms
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    notification_type: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    template_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

    token_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # Optional link to the user-visible in-app Notification row
    notification_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("notifications.id", ondelete="SET NULL"), nullable=True, index=True,
    )

    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)

    # NTF-005 duplicate suppression: same (channel, dedup_key) inside the
    # dedup window collapses into one delivery.
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OutboxStatus.PENDING, index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped["datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_attempt_at: Mapped["datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    sent_at: Mapped["datetime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<NotificationOutbox id={self.id} {self.channel} {self.status} tries={self.attempts}>"
