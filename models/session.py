"""
models/session.py
─────────────────
Server-side session store for authenticated principals (admin, org, employee, user).
Enables server-side revocation, session timeout, and multi-device management.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Session(Base):
    __tablename__ = "sessions"

    # UUID primary key passed as 'jti' in JWT
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    principal_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    principal_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<Session id={self.id[:8]}... type={self.principal_type} "
            f"id={self.principal_id} revoked={self.is_revoked}>"
        )
