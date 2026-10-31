"""
app/schemas/schedule.py
───────────────────────
Pydantic schemas for schedule administration (Phase 3 — weekly schedules,
schedule exceptions and capacity rules, SCH-001/002/003/004).

The polymorphic `(scope_level, scope_id, organization_id)` scope tuple is
validated by the service layer (scope target must exist and belong to the
caller's organization — models intentionally do not FK `scope_id`).
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from models.capacity_rule import BookingSource, CapacityLimitType
from models.schedule_exception import ScheduleExceptionType
from models.weekly_schedule import ScheduleScopeLevel


# ── Weekly schedules ──────────────────────────────────────────────────────────

class WeeklyScheduleCreate(BaseModel):
    scope_level: ScheduleScopeLevel = ScheduleScopeLevel.ORGANIZATION
    scope_id: int = Field(..., ge=1)
    day_of_week: int = Field(..., ge=0, le=6, description="0 = Monday … 6 = Sunday")
    is_closed: bool = False
    open_time: Optional[time] = None
    close_time: Optional[time] = None
    slot_interval_minutes: Optional[int] = Field(None, ge=1, le=1440)


class WeeklyScheduleUpdate(BaseModel):
    scope_level: Optional[ScheduleScopeLevel] = None
    scope_id: Optional[int] = Field(None, ge=1)
    day_of_week: Optional[int] = Field(None, ge=0, le=6)
    is_closed: Optional[bool] = None
    open_time: Optional[time] = None
    close_time: Optional[time] = None
    slot_interval_minutes: Optional[int] = Field(None, ge=1, le=1440)


class WeeklyScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    scope_level: str
    scope_id: int
    day_of_week: int
    is_closed: bool
    open_time: Optional[time]
    close_time: Optional[time]
    slot_interval_minutes: Optional[int]
    created_at: datetime
    updated_at: datetime


# ── Schedule exceptions ───────────────────────────────────────────────────────

class ScheduleExceptionCreate(BaseModel):
    scope_level: ScheduleScopeLevel = ScheduleScopeLevel.ORGANIZATION
    scope_id: int = Field(..., ge=1)
    exception_date: date
    exception_type: ScheduleExceptionType
    reason: str = Field(..., min_length=3, max_length=500)
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    capacity_factor: Optional[Decimal] = Field(None, gt=0, le=1)
    is_active: bool = True


class ScheduleExceptionUpdate(BaseModel):
    exception_date: Optional[date] = None
    exception_type: Optional[ScheduleExceptionType] = None
    reason: Optional[str] = Field(None, min_length=3, max_length=500)
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    capacity_factor: Optional[Decimal] = Field(None, gt=0, le=1)
    is_active: Optional[bool] = None


class ScheduleExceptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    scope_level: str
    scope_id: int
    exception_date: date
    exception_type: str
    reason: str
    opens_at: Optional[time]
    closes_at: Optional[time]
    capacity_factor: Optional[Decimal]
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ── Capacity rules ────────────────────────────────────────────────────────────

class CapacityRuleCreate(BaseModel):
    scope_level: ScheduleScopeLevel = ScheduleScopeLevel.ORGANIZATION
    scope_id: int = Field(..., ge=1)
    limit_type: CapacityLimitType
    booking_source: BookingSource = BookingSource.ALL
    max_count: int = Field(..., ge=0, le=1000000)
    is_active: bool = True


class CapacityRuleUpdate(BaseModel):
    scope_level: Optional[ScheduleScopeLevel] = None
    scope_id: Optional[int] = Field(None, ge=1)
    limit_type: Optional[CapacityLimitType] = None
    booking_source: Optional[BookingSource] = None
    max_count: Optional[int] = Field(None, ge=0, le=1000000)
    is_active: Optional[bool] = None


class CapacityRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    scope_level: str
    scope_id: int
    limit_type: str
    booking_source: str
    max_count: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ── Schedule-change impact workflow (SCH-007 / Decision 11) ──────────────────

class ScheduleChangePreviewRequest(BaseModel):
    """Propose (NOT apply) a schedule configuration change.

    `data` carries the same payload the corresponding CRUD endpoint would
    accept (WeeklyScheduleCreate/Update/Delete semantics, keyed by
    `edit_type`); it is validated with the identical validation cores so a
    confirmed preview applies exactly what it previewed."""
    entity: str = Field(..., description="weekly_schedule | schedule_exception | capacity_rule")
    edit_type: str = Field(..., description="create | update | delete")
    target_id: Optional[int] = Field(None, ge=1)
    data: dict = Field(default_factory=dict)


class ScheduleChangeDecisionIn(BaseModel):
    decision: str = Field(..., description="honor | reschedule | cancel")
    reason: Optional[str] = Field(None, max_length=500)


class ScheduleChangeDecisionsRequest(BaseModel):
    decisions: dict[str, ScheduleChangeDecisionIn]


class ScheduleChangePreviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    entity: str
    edit_type: str
    target_id: Optional[int]
    status: str
    proposal: dict
    impact: dict
    decisions: Optional[dict]
    impact_digest: str
    affected_counter_ids: list[int]
    horizon_start: date
    horizon_days: int
    expires_at: datetime
    created_by: Optional[str]
    confirmed_at: Optional[datetime]
    applied_summary: Optional[dict]
    created_at: datetime
    updated_at: datetime
