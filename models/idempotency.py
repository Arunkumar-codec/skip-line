"""
models/idempotency.py
─────────────────────
Phase 8 — Durable idempotency for booking retries (DB-001 continuation).

`tokens.client_reference` already provides channel-level idempotency for
kiosk/walk-in retries (KSK-005 / PHY-001). This table generalises the
mechanism to API clients: a caller supplies `Idempotency-Key`;
`idempotency_service.begin()` get-or-inserts a row under a UNIQUE
(organization_id, key) constraint and either
  - creates a fresh OPEN row (caller proceeds and records the result with
    `complete()`), or
  - returns the existing row, which the caller replays when COMPLETED or
    rejects with a 409 while still OPEN (a concurrent retry of the same
    operation is in flight).

Money paths (payments/refunds/webhooks) retain their existing
state-check idempotency — this table is for queue/token mutations.
"""

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from models.mixins import TimestampMixin


class IdempotencyState:
    OPEN = "open"
    COMPLETED = "completed"
    FAILED = "failed"

    ALL = (OPEN, COMPLETED, FAILED)


class IdempotencyKey(Base, TimestampMixin):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_idempotency_keys"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    # SHA-256 of the canonical request payload (retry-with-different-body detection)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=IdempotencyState.OPEN, index=True,
    )

    # Result snapshot for replay: {"token_id": ..., "token_number": ...}
    response_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    principal_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<IdempotencyKey org={self.organization_id} {self.key} {self.status}>"
