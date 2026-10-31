"""
models/schedule_exception.py
────────────────────────────
Date-specific overrides of the recurring weekly schedule (SCH-003):
holiday closures, emergency closures, maintenance, special hours,
reduced capacity, and counter/service closure — each carrying a
user-facing reason.

Same scoping design as `weekly_schedules`:
`(scope_level, scope_id, organization_id)` with a polymorphic,
non-FK `scope_id` and a concrete `organization_id` FK for tenant
isolation (see models/weekly_schedule.py for the rationale).
"""

import enum
import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin
from models.weekly_schedule import ScheduleScopeLevel, _enum_values


class ScheduleExceptionType(str, enum.Enum):
    HOLIDAY = "holiday"
    EMERGENCY_CLOSURE = "emergency_closure"
    MAINTENANCE = "maintenance"
    SPECIAL_HOURS = "special_hours"
    REDUCED_CAPACITY = "reduced_capacity"
    COUNTER_CLOSURE = "counter_closure"
    SERVICE_CLOSURE = "service_closure"


class ScheduleException(Base, TimestampMixin):
    __tablename__ = "schedule_exceptions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "scope_level", "scope_id", "exception_date",
            name="uq_schedule_exception_org_scope_date",
        ),
        CheckConstraint(
            "capacity_factor IS NULL OR (capacity_factor > 0 AND capacity_factor <= 1)",
            name="ck_schedule_exception_capacity_factor",
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

    exception_date: Mapped[datetime.date] = mapped_column(Date(), nullable=False)
    exception_type: Mapped[str] = mapped_column(
        Enum(ScheduleExceptionType, values_callable=_enum_values),
        nullable=False,
    )

    # User-facing reason (SCH-003) — shown on calendars and booking screens
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # For SPECIAL_HOURS: operating window for that date only
    opens_at: Mapped[datetime.time | None] = mapped_column(Time(), nullable=True)
    closes_at: Mapped[datetime.time | None] = mapped_column(Time(), nullable=True)

    # For REDUCED_CAPACITY: multiplier applied to normal capacity (e.g. 0.50)
    capacity_factor: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization")

    def __repr__(self) -> str:
        return (
            f"<ScheduleException id={self.id} scope={self.scope_level}:{self.scope_id} "
            f"date={self.exception_date} type={self.exception_type}>"
        )
