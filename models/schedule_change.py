"""
models/schedule_change.py
────────────────────────
Schedule-change impact workflow records (SCH-007, OPEN_DECISIONS Decision 11).

One row per proposed schedule configuration change (weekly schedule,
schedule exception or capacity rule) that an organization administrator
submitted for impact review. The row persists:

- the proposed change (`proposal`),
- the computed impact on generated `time_slots` and confirmed bookings
  (`impact`),
- the server-computed fingerprint of the affected state
  (`impact_digest`) — recomputed and compared at confirmation time so a
  stale preview (config changed, slots generated/modified, bookings
  moved/cancelled, occupancy changed since the preview was created)
  can never be applied,
- the administrator's explicit per-booking resolution decisions
  (`decisions`: honor / reschedule / cancel, with actor, time, reason
  and outcome) — recorded through the `resolve` step, executed atomically
  at `confirm`.

Decision 16 boundary: this table never releases slot capacity.
`reschedule` decisions are recorded but blocked at execution (releasing
the old slot's occupied place requires an approved capacity-release
policy that does not exist yet). Cancelled tokens keep their place
consumed in `time_slots.booked_count`, and Decision 13A daily-quota
semantics are untouched.
"""

import datetime as _dt
import enum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin
from models.weekly_schedule import _enum_values


class ScheduleChangeEntity(str, enum.Enum):
    """Which schedule configuration entity the proposal targets."""

    WEEKLY_SCHEDULE = "weekly_schedule"
    SCHEDULE_EXCEPTION = "schedule_exception"
    CAPACITY_RULE = "capacity_rule"


class ScheduleChangeStatus(str, enum.Enum):
    """Preview lifecycle (Decision 11: explicit, audited, no silent apply)."""

    PENDING = "pending"          # awaiting decisions and/or confirmation
    CONFIRMED = "confirmed"      # applied exactly once, atomically
    REJECTED = "rejected"        # organization decided not to proceed
    STALE = "stale"              # fingerprint mismatch — state changed
    EXPIRED = "expired"          # TTL elapsed without confirmation


class ScheduleChangePreview(Base, TimestampMixin):
    __tablename__ = "schedule_change_previews"
    __table_args__ = (
        CheckConstraint(
            "target_id >= 0",
            name="ck_schedule_change_target_id",
        ),
        # status + expires_at/confirmed_at are plain columns (checked in the
        # service layer with conditional UPDATEs for concurrency safety).
        Index(
            "ix_schedule_change_org_status",
            "organization_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    entity: Mapped[str] = mapped_column(
        Enum(ScheduleChangeEntity, values_callable=_enum_values), nullable=False,
    )
    edit_type: Mapped[str] = mapped_column(
        Enum("create", "update", "delete", name="schedule_change_edit_type"), nullable=False,
    )
    # Target configuration row (update/delete); NULL for create proposals.
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(
        Enum(ScheduleChangeStatus, values_callable=_enum_values),
        nullable=False, default=ScheduleChangeStatus.PENDING, index=True,
    )

    # Canonical proposed-row fields (validated at preview time; the same
    # raw `data` template is re-validated when re-apply is confirmed).
    proposal: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Computed impact report (slots/bookings/state deltas/conflicts).
    impact: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Per-booking resolution decisions, keyed by token id (str):
    # {decision, reason, resolved_by, resolved_at, outcome?, outcome_detail?}
    decisions: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ── Stale-preview protection ─────────────────────────────────────────────
    # Fingerprint over (a) the affected counters' generated slots within the
    # preview horizon including occupancy and their token states, and (b) the
    # tenant's schedule configuration rows (id + updated_at). Recomputed and
    # compared server-side at confirmation — never trusted from the client.
    impact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Scope + horizon used to recompute the digest deterministically.
    affected_counter_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    horizon_start: Mapped[_dt.date] = mapped_column(Date(), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)

    # TTL / completion bookkeeping
    expires_at: Mapped[_dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Summary of what the confirmation actually applied/decided/notified.
    applied_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Relationship
    organization: Mapped["Organization"] = relationship("Organization")

    def __repr__(self) -> str:
        return (
            f"<ScheduleChangePreview id={self.id} org={self.organization_id} "
            f"{self.edit_type}:{self.entity} status={self.status}>"
        )
