"""
models/setting.py
─────────────────
Key-value configuration store scoped per organization.
Enables per-org feature flags without schema changes.

Example keys:
  "payment_enabled"         → "true"
  "sms_enabled"             → "false"
  "kiosk_enabled"           → "true"
  "token_recall_limit"      → "3"
  "announcement_text"       → "Welcome to City Hospital"
"""

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_org_setting_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization", back_populates="settings")

    def __repr__(self) -> str:
        return f"<Setting org={self.organization_id} {self.key!r}={self.value!r}>"
