"""
models/wait_time_estimate.py
────────────────────────────
Phase 10 (MASTER §16.6): persisted waiting-time prediction snapshots.

One row per estimation event for a token:
  - snapshot_kind="initial"  → recorded inside the booking transaction when
                               the token is created (the §16.6 anchor that is
                               later compared against the actual service
                               start, `tokens.called_at`).
  - snapshot_kind="recalc"   → material recalculations recorded after queue
                               events (call/complete/skip/cancel/hold).

Never updated — appended only (audit semantics, like token_history).
Predicted values may be NULL: an honest `insufficient_data` estimate
(§16.1 — never invent precision) is still persisted so accuracy reporting
can distinguish "estimated" from "not estimable".
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class WaitTimeEstimate(Base, TimestampMixin):
    __tablename__ = "wait_time_estimates"
    __table_args__ = (
        # accuracy lookups: per token, chronological; per org for reports
        Index("ix_wte_token_kind", "token_id", "snapshot_kind", "id"),
        Index("ix_wte_org_created", "organization_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False
    )
    # SET NULL so a counter deletion never blocks token cleanup (queue_logs
    # precedent); accuracy math does not need the counter.
    counter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="SET NULL"), nullable=True
    )

    # "initial" | "recalc"
    snapshot_kind: Mapped[str] = mapped_column(String(20), nullable=False)

    # Predicted wait (minutes) — NULL when the honest answer is
    # "insufficient data" (§16.1/§16.3: never fabricate a number).
    predicted_wait_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    predicted_range_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    predicted_range_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # "estimate" | "insufficient_data"
    estimation_status: Mapped[str] = mapped_column(String(30), nullable=False)
    # "historical" | "blended" | "configured" | "insufficient_data"
    estimation_basis: Mapped[str] = mapped_column(String(30), nullable=False)
    # Human-readable explainability string (§16.3/§16.4), e.g.
    # "2 tokens ahead across 1 active counter (~6 min median service time)".
    estimation_explanation: Mapped[str] = mapped_column(String(500), nullable=False)

    # Raw estimator inputs, persisted so a snapshot stays explainable even
    # after the underlying queue/history drifts away.
    people_ahead: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_counter_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Point-in-time of the estimation itself (UTC); TimestampMixin rows are
    # bookkeeping. indexed for windowed accuracy queries.
    estimated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    token = relationship("Token", back_populates="wait_time_estimates")

    def __repr__(self) -> str:
        return (
            f"<WaitTimeEstimate id={self.id} token={self.token_id} "
            f"kind={self.snapshot_kind!r} predicted={self.predicted_wait_minutes}>"
        )
