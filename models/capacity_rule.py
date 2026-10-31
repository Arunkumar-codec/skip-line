"""
models/capacity_rule.py
───────────────────────
Configurable capacity limits (SCH-004): daily, slot, counter, service,
source, user and priority-reserve limits.

Same scoping design as `weekly_schedules` / `schedule_exceptions`:
`(scope_level, scope_id, organization_id)` with a polymorphic, non-FK
`scope_id` and a concrete `organization_id` FK for tenant isolation.

`booking_source` allows source-based allocation (CTR-003): a rule can
target one source (`online`, `kiosk`, `walk_in`, `employee`) or all
sources. `ALL` is a stored value — never NULL — so the uniqueness
constraint behaves identically on MySQL and SQLite.

Rules are enforced by the booking engine as independent layered checks;
the daily-limit behavior of existing counters (Decision 13A) is a
separate mechanism and is unchanged by this table.
"""

import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin
from models.weekly_schedule import ScheduleScopeLevel, _enum_values


class CapacityLimitType(str, enum.Enum):
    DAILY_TOKEN = "daily_token"
    SLOT_CAPACITY = "slot_capacity"
    WALK_IN_QUOTA = "walk_in_quota"
    PER_USER = "per_user"
    PRIORITY_RESERVE = "priority_reserve"


class BookingSource(str, enum.Enum):
    ALL = "all"
    ONLINE = "online"
    KIOSK = "kiosk"
    WALK_IN = "walk_in"
    EMPLOYEE = "employee"


class CapacityRule(Base, TimestampMixin):
    __tablename__ = "capacity_rules"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "scope_level", "scope_id", "limit_type", "booking_source",
            name="uq_capacity_rule_org_scope_type_source",
        ),
        CheckConstraint(
            "max_count >= 0",
            name="ck_capacity_rule_max_count",
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

    limit_type: Mapped[str] = mapped_column(
        Enum(CapacityLimitType, values_callable=_enum_values),
        nullable=False,
    )
    booking_source: Mapped[str] = mapped_column(
        Enum(BookingSource, values_callable=_enum_values),
        nullable=False,
        default=BookingSource.ALL,
    )
    max_count: Mapped[int] = mapped_column(Integer, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization")

    def __repr__(self) -> str:
        return (
            f"<CapacityRule id={self.id} scope={self.scope_level}:{self.scope_id} "
            f"type={self.limit_type} source={self.booking_source} max={self.max_count}>"
        )
