"""
models/kiosk_device.py
──────────────────────
Physical self-service kiosk devices (KSK-001..006).

Each kiosk device is assigned to an organization and optional branch.
Supports secure pairing via 6-digit numeric PIN, device credentials,
heartbeat tracking, and remote deactivation/revocation by the organization head.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class KioskDevice(Base, TimestampMixin):
    __tablename__ = "kiosk_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    branch_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("branches.id", ondelete="SET NULL"), nullable=True, index=True
    )

    device_name: Mapped[str] = mapped_column(String(100), nullable=False)
    device_code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    api_key_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 6-digit pairing PIN workflow (Open Decision 7)
    pairing_code: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    pairing_code_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Configuration restrictions: allowed service IDs and counter IDs
    allowed_services: Mapped[list | None] = mapped_column(JSON, nullable=True)
    allowed_counters: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization")
    branch: Mapped["Branch | None"] = relationship("Branch")

    def __repr__(self) -> str:
        return f"<KioskDevice id={self.id} code={self.device_code!r} active={self.is_active}>"

