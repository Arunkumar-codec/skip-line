"""
models/weekly_schedule.py
────────────────────────
Recurring weekly operating hours, scoped to an organization, branch,
counter or service (SCH-002).

Scoping uses a single `(scope_level, scope_id, organization_id)` tuple
instead of three nullable FK columns so that the tenant-aware uniqueness
constraint
`(organization_id, scope_level, scope_id, day_of_week)` is enforceable
identically on MySQL and SQLite (NULL values are treated as distinct by
both engines, which would break a uniqueness guarantee on nullable FK
columns). `organization_id` is part of the uniqueness key because
`scope_id` values are only unique within their tenant — without it, two
organizations' org-level schedules (scope_id = each org's own PK) would
collide.

Precedence rule (resolved by the scheduling service, not stored here):
    service > counter > branch > organization

`scope_id` is intentionally NOT a foreign key: it is a polymorphic
reference whose target table depends on `scope_level`. Tenant isolation
is enforced through the concrete `organization_id` FK and by the
service layer, which must validate that `scope_id` belongs to
`organization_id`.
"""

import enum
from datetime import time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class ScheduleScopeLevel(str, enum.Enum):
    ORGANIZATION = "org"
    BRANCH = "branch"
    COUNTER = "counter"
    SERVICE = "service"


# SQLAlchemy's Enum(enum_class) persists member *names* ("ORGANIZATION")
# by default; persist the lowercase *values* so model DDL matches
# migration 0005 on both MySQL and SQLite.
def _enum_values(enum_cls) -> list[str]:
    return [member.value for member in enum_cls]


class WeeklySchedule(Base, TimestampMixin):
    __tablename__ = "weekly_schedules"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "scope_level", "scope_id", "day_of_week",
            name="uq_weekly_schedule_org_scope_day",
        ),
        CheckConstraint(
            "day_of_week >= 0 AND day_of_week <= 6",
            name="ck_weekly_schedule_dow",
        ),
        # If the day is not closed, opening hours are mandatory and ordered.
        CheckConstraint(
            "is_closed = 1 OR (open_time IS NOT NULL AND close_time IS NOT NULL "
            "AND open_time < close_time)",
            name="ck_weekly_schedule_hours",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Polymorphic scope target (see module docstring)
    scope_level: Mapped[str] = mapped_column(
        Enum(ScheduleScopeLevel, values_callable=_enum_values),
        nullable=False,
        default=ScheduleScopeLevel.ORGANIZATION,
    )
    scope_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # 0 = Monday … 6 = Sunday
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)

    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    open_time: Mapped[time | None] = mapped_column(Time(), nullable=True)
    close_time: Mapped[time | None] = mapped_column(Time(), nullable=True)

    # Appointment-slot interval in minutes for this scope (15/30/60/custom);
    # NULL = day-queue operating mode (no discrete slots).
    slot_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization")

    def __repr__(self) -> str:
        return (
            f"<WeeklySchedule id={self.id} scope={self.scope_level}:{self.scope_id} "
            f"dow={self.day_of_week} closed={self.is_closed}>"
        )
