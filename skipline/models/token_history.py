"""
models/token_history.py
────────────────────────
Immutable audit log of every status change for a token.
Records who changed the status and when, enabling full replay.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TokenHistory(Base):
    __tablename__ = "token_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str] = mapped_column(String(30), nullable=False)

    # Who changed the status: "admin" | "organization" | "user" | "system"
    changed_by_role: Mapped[str] = mapped_column(String(30), nullable=False)
    changed_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    token: Mapped["Token"] = relationship("Token", back_populates="history")

    def __repr__(self) -> str:
        return (
            f"<TokenHistory id={self.id} token={self.token_id} "
            f"{self.old_status}→{self.new_status}>"
        )
