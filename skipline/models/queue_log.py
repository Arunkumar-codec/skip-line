"""
models/queue_log.py
────────────────────
Append-only audit log for every action performed on the queue.
Used for analytics, dispute resolution, and compliance.
Never updated — only inserted.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class QueueLog(Base):
    __tablename__ = "queue_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    counter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="SET NULL"), nullable=True
    )
    token_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Action performed, e.g. "token_created", "token_called", "token_completed"
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)

    # Who did this: "admin" | "organization" | "user" | "system" | "kiosk"
    performed_by_role: Mapped[str] = mapped_column(String(30), nullable=False)
    performed_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Additional context (JSON string, e.g. old/new status, queue position)
    meta: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="queue_logs")
    counter: Mapped["Counter | None"] = relationship("Counter", back_populates="queue_logs")
    token: Mapped["Token | None"] = relationship("Token", back_populates="queue_logs")

    def __repr__(self) -> str:
        return f"<QueueLog id={self.id} action={self.action!r} token={self.token_id}>"
