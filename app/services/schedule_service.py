"""
app/services/schedule_service.py
────────────────────────────────
Schedule resolution, availability generation and slot generation
(Phase 3, Task 2 — OPEN_DECISIONS Decisions 10-12, 15).

Scope of this module:
- Weekly-schedule resolution with precedence service > counter > branch >
  organization (SCH-002).
- Date-exception application: closures (holiday, emergency, maintenance,
  counter/service closure), special hours and reduced capacity (SCH-003).
- Timezone handling: UTC storage for generated slot times, IANA timezone
  per organization with nullable branch override, `Asia/Kolkata` default
  (SCH-009, Decision 12).
- Representability constraint: slot boundaries are stored as the pair
  (slot_date, UTC time-of-day); a local wall-clock time whose UTC
  conversion lands on a different calendar date cannot be represented, so
  generation raises HTTP 400 for such configurations (review defect D1,
  documented in OPEN_DECISIONS §4).
- Slot generation for counters with `queue_mode = slots` (Decision 10),
  idempotent by `(counter_id, slot_date, start_time)`.
- Published availability calendar for at least one month
  (PUBLISH_HORIZON_DAYS), with safe on-demand generation for specific
  later dates up to MAX_ADVANCE_BOOKING_DAYS (SCH-001, SCH-005).

Out of scope here (later Phase 3 tasks): atomic booking enforcement in
`book_token`, schedule-change impact workflow (Decision 11), API/UI.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.branch import Branch
from models.capacity_rule import BookingSource, CapacityLimitType, CapacityRule
from models.counter import Counter, CounterStatus, QueueMode
from models.organization import OrgStatus, Organization
from models.schedule_exception import ScheduleException, ScheduleExceptionType
from models.service import Service
from models.time_slot import TimeSlot
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule


# ── Configuration constants ───────────────────────────────────────────────────

DEFAULT_TIMEZONE = "Asia/Kolkata"          # Decision 12
PUBLISH_HORIZON_DAYS = 31                  # SCH-001: publish >= 1 month
MAX_ADVANCE_BOOKING_DAYS = 60              # future-booking horizon guard (SCH-005)
DEFAULT_SLOT_CAPACITY = 10                 # used when no slot_capacity rule exists
LIMITED_CAPACITY_RATIO = Decimal("0.30")   # remaining < 30% → LIMITED (SCH-008)


class AvailabilityState(str, enum.Enum):
    """User-facing calendar states (SCH-008)."""

    AVAILABLE = "available"
    LIMITED = "limited"
    FULL = "full"
    CLOSED = "closed"
    HOLIDAY = "holiday"
    NOT_OPEN = "not_open"
    SERVICE_UNAVAILABLE = "service_unavailable"


_CLOSURE_EXCEPTION_TYPES = {
    ScheduleExceptionType.HOLIDAY,
    ScheduleExceptionType.EMERGENCY_CLOSURE,
    ScheduleExceptionType.MAINTENANCE,
    ScheduleExceptionType.COUNTER_CLOSURE,
    ScheduleExceptionType.SERVICE_CLOSURE,
}

# Precedence order: most specific first (SCH-002)
_PRECEDENCE = (
    ScheduleScopeLevel.SERVICE,
    ScheduleScopeLevel.COUNTER,
    ScheduleScopeLevel.BRANCH,
    ScheduleScopeLevel.ORGANIZATION,
)


@dataclass
class DayAvailability:
    """Resolved availability for one scope chain on one business date."""

    state: AvailabilityState
    timezone_name: str
    reason: Optional[str] = None
    opens_at: Optional[time] = None        # local business hours
    closes_at: Optional[time] = None
    capacity_factor: Optional[Decimal] = None
    slot_interval_minutes: Optional[int] = None
    queue_mode: QueueMode = QueueMode.DAY_QUEUE
    is_legacy_day_queue: bool = False      # no schedule published anywhere
    exception: Optional[ScheduleException] = None


@dataclass
class ShadowScheduleRow:
    """Non-persisted proposed weekly-schedule row (SCH-007 overlay).

    The schedule-change impact preview resolves "what would this day look
    like if the proposed configuration change were applied?" through the
    SAME resolution functions, passing shadow rows that win over database
    rows at their `(scope_level, scope_id, day_of_week)` key. Shadow rows
    never touch the session, database or any generated slot.
    """

    is_closed: bool = False
    open_time: Optional[time] = None
    close_time: Optional[time] = None
    slot_interval_minutes: Optional[int] = None


@dataclass
class ShadowExceptionRow:
    """Non-persisted proposed schedule-exception row (SCH-007 overlay)."""

    exception_type: ScheduleExceptionType
    reason: str = ""
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    capacity_factor: Optional[Decimal] = None
    is_active: bool = True


# Overlay key: (scope_level VALUE, scope_id, day_of_week | target_date).
# A `None` value marks the row as REMOVED at that key (delete/deactivate /
# moved-away) — resolution treats that scope level as having no row.
ROScheduleOverlay = dict[tuple[str, int, int], Optional[ShadowScheduleRow]]
ROExceptionOverlay = dict[tuple[str, int, date], Optional[ShadowExceptionRow]]


# ── Timezone helpers (SCH-009) ────────────────────────────────────────────────

def get_timezone_name(organization: Organization, branch: Optional[Branch]) -> str:
    """Branch override wins; org default next; hard default last."""
    if branch is not None and branch.timezone:
        return branch.timezone
    return organization.timezone or DEFAULT_TIMEZONE


def get_tenant_today(organization: Organization, branch: Optional[Branch]) -> date:
    """Current business-local date in the tenant timezone (SCH-009)."""
    return datetime.now(_zone(get_timezone_name(organization, branch))).date()


def _utcnow() -> datetime:
    """Current UTC instant (injectable clock seam — cutoff tests freeze
    this instead of patching datetime itself)."""
    return datetime.now(dt_timezone.utc)


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid IANA timezone: {tz_name!r}",
        )


def local_to_utc_datetime(target_date: date, local_time: time, tz_name: str) -> datetime:
    """Convert a wall-clock business time in the tenant timezone to an
    aware UTC datetime (Decision 12: timestamps stored in UTC)."""
    aware = datetime.combine(target_date, local_time, tzinfo=_zone(tz_name))
    return aware.astimezone(dt_timezone.utc)


def utc_slot_time(target_date: date, local_time: time, tz_name: str) -> Optional[time]:
    """UTC time-of-day for a business-local slot boundary, or None when the
    conversion lands on a different UTC calendar date. The stored pair
    (slot_date, start_time) can only represent same-UTC-date instants
    (OPEN_DECISIONS §4 data-model notes)."""
    utc_dt = local_to_utc_datetime(target_date, local_time, tz_name)
    if utc_dt.date() != target_date:
        return None
    return utc_dt.time()


def utc_pair_to_local_time(target_date: date, utc_time: time, tz_name: str) -> time:
    """Inverse of `utc_slot_time` (SCH-009 rendering): map the stored
    (slot_date, UTC time-of-day) pair back to the business-local wall
    clock in the tenant timezone. The stored pair can only exist when the
    forward conversion landed on the same UTC calendar date, so the
    round-trip is exact."""
    aware = datetime.combine(target_date, utc_time, tzinfo=dt_timezone.utc)
    return aware.astimezone(_zone(tz_name)).time()


def utc_pair_to_utc_datetime(target_date: date, utc_time: time) -> datetime:
    """Absolute UTC instant for a stored (slot_date, UTC time) pair."""
    return datetime.combine(target_date, utc_time, tzinfo=dt_timezone.utc)


# ── Scope-row resolution ──────────────────────────────────────────────────────

def _build_scope_ids(
    organization: Organization,
    branch: Optional[Branch],
    counter: Optional[Counter],
    service: Optional[Service],
) -> dict[ScheduleScopeLevel, int]:
    scope_ids: dict[ScheduleScopeLevel, int] = {
        ScheduleScopeLevel.ORGANIZATION: organization.id
    }
    if branch is not None:
        scope_ids[ScheduleScopeLevel.BRANCH] = branch.id
    if counter is not None:
        scope_ids[ScheduleScopeLevel.COUNTER] = counter.id
    if service is not None:
        scope_ids[ScheduleScopeLevel.SERVICE] = service.id
    return scope_ids


async def resolve_weekly_schedule(
    db: AsyncSession,
    organization_id: int,
    day_of_week: int,
    scope_ids: dict[ScheduleScopeLevel, int],
    schedule_overlay: Optional[ROScheduleOverlay] = None,
) -> WeeklySchedule | ShadowScheduleRow | None:
    """Most specific published weekly row for `day_of_week` wins.

    `schedule_overlay` (SCH-007 impact preview): when a `(scope_level
    value, scope_id, day_of_week)` key is present in the overlay, its
    shadow row wins at that level (None = row removed there). Without an
    overlay the function reads only committed database rows — existing
    callers are unaffected."""
    for level in _PRECEDENCE:
        scope_id = scope_ids.get(level)
        if scope_id is None:
            continue
        if schedule_overlay is not None:
            shadow = schedule_overlay.get((level.value, scope_id, day_of_week))
            if shadow is not None:
                return shadow
            if (level.value, scope_id, day_of_week) in schedule_overlay:
                continue  # explicitly removed at this level
        stmt = select(WeeklySchedule).where(
            WeeklySchedule.organization_id == organization_id,
            WeeklySchedule.scope_level == level,
            WeeklySchedule.scope_id == scope_id,
            WeeklySchedule.day_of_week == day_of_week,
        )
        row = (await db.execute(stmt)).scalars().first()
        if row is not None:
            return row
    return None


async def resolve_schedule_exception(
    db: AsyncSession,
    organization_id: int,
    target_date: date,
    scope_ids: dict[ScheduleScopeLevel, int],
    exception_overlay: Optional[ROExceptionOverlay] = None,
) -> ScheduleException | ShadowExceptionRow | None:
    """Most specific active date exception wins.

    `exception_overlay` (SCH-007 impact preview): shadow rows
    (or an explicit removal marker) win at their
    `(scope_level value, scope_id, date)` key without any database
    write."""
    for level in _PRECEDENCE:
        scope_id = scope_ids.get(level)
        if scope_id is None:
            continue
        if exception_overlay is not None:
            key = (level.value, scope_id, target_date)
            if key in exception_overlay:
                shadow = exception_overlay[key]
                if shadow is not None:
                    return shadow
                continue  # explicitly removed at this level
        stmt = select(ScheduleException).where(
            ScheduleException.organization_id == organization_id,
            ScheduleException.scope_level == level,
            ScheduleException.scope_id == scope_id,
            ScheduleException.exception_date == target_date,
            ScheduleException.is_active.is_(True),
        )
        row = (await db.execute(stmt)).scalars().first()
        if row is not None:
            return row
    return None


async def _any_schedule_published(
    db: AsyncSession,
    organization_id: int,
    scope_ids: dict[ScheduleScopeLevel, int],
) -> bool:
    """True if the tenant has published at least one weekly row anywhere in
    this scope chain (any day of week)."""
    conds = [
        and_(
            WeeklySchedule.scope_level == level,
            WeeklySchedule.scope_id == scope_ids[level],
        )
        for level in _PRECEDENCE
        if level in scope_ids
    ]
    stmt = (
        select(WeeklySchedule.id)
        .where(WeeklySchedule.organization_id == organization_id, or_(*conds))
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first() is not None


async def _resolve_slot_interval(
    db: AsyncSession,
    organization_id: int,
    day_of_week: int,
    scope_ids: dict[ScheduleScopeLevel, int],
    schedule_overlay: Optional[ROScheduleOverlay] = None,
) -> Optional[int]:
    """Most specific row that carries a slot interval (may be less specific
    than the winning hours row). Overlay-aware for SCH-007 previews."""
    for level in _PRECEDENCE:
        scope_id = scope_ids.get(level)
        if scope_id is None:
            continue
        if schedule_overlay is not None:
            key = (level.value, scope_id, day_of_week)
            if key in schedule_overlay:
                shadow = schedule_overlay[key]
                if shadow is not None and shadow.slot_interval_minutes is not None:
                    return shadow.slot_interval_minutes
                continue  # removed or interval-cleared at this level
        stmt = select(WeeklySchedule).where(
            WeeklySchedule.organization_id == organization_id,
            WeeklySchedule.scope_level == level,
            WeeklySchedule.scope_id == scope_id,
            WeeklySchedule.day_of_week == day_of_week,
            WeeklySchedule.slot_interval_minutes.is_not(None),
        )
        row = (await db.execute(stmt)).scalars().first()
        if row is not None:
            return row.slot_interval_minutes
    return None


# ── Entity state guards ───────────────────────────────────────────────────────

def _entity_state_block(
    organization: Organization,
    branch: Optional[Branch],
    counter: Optional[Counter],
    service: Optional[Service],
) -> Optional[tuple[AvailabilityState, str]]:
    if not organization.is_active or organization.status != OrgStatus.APPROVED:
        return (AvailabilityState.NOT_OPEN, "Organization is not currently operational")
    if branch is not None and not branch.is_active:
        return (AvailabilityState.NOT_OPEN, "This branch is not currently operational")
    if counter is not None and counter.status != CounterStatus.ACTIVE:
        return (AvailabilityState.NOT_OPEN, "This counter is not currently operational")
    if service is not None and not service.is_active:
        return (
            AvailabilityState.SERVICE_UNAVAILABLE,
            "This service is currently unavailable",
        )
    return None


# ── Day resolution ────────────────────────────────────────────────────────────

async def resolve_day_availability(
    db: AsyncSession,
    organization: Organization,
    target_date: date,
    branch: Optional[Branch] = None,
    counter: Optional[Counter] = None,
    service: Optional[Service] = None,
    schedule_overlay: Optional[ROScheduleOverlay] = None,
    exception_overlay: Optional[ROExceptionOverlay] = None,
) -> DayAvailability:
    """Resolve the availability state for one business date (SCH-008).

    `schedule_overlay` / `exception_overlay` (SCH-007 impact preview):
    non-persisted proposed rows that win over database rows inside the
    same resolution logic — no second resolution engine, no writes."""
    tz_name = get_timezone_name(organization, branch)
    _zone(tz_name)  # validate the stored IANA identifier early (SCH-009)
    queue_mode = counter.queue_mode if counter is not None else QueueMode.DAY_QUEUE

    blocked = _entity_state_block(organization, branch, counter, service)
    if blocked is not None:
        return DayAvailability(
            state=blocked[0], reason=blocked[1], timezone_name=tz_name,
            queue_mode=queue_mode,
        )

    scope_ids = _build_scope_ids(organization, branch, counter, service)
    day_of_week = target_date.weekday()  # Monday = 0 … Sunday = 6

    exception = await resolve_schedule_exception(
        db, organization.id, target_date, scope_ids,
        exception_overlay=exception_overlay,
    )
    schedule = await resolve_weekly_schedule(
        db, organization.id, day_of_week, scope_ids,
        schedule_overlay=schedule_overlay,
    )

    state: Optional[AvailabilityState] = None
    reason: Optional[str] = None
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    capacity_factor: Optional[Decimal] = None

    if exception is not None:
        if exception.exception_type in _CLOSURE_EXCEPTION_TYPES:
            state = (
                AvailabilityState.HOLIDAY
                if exception.exception_type == ScheduleExceptionType.HOLIDAY
                else AvailabilityState.CLOSED
            )
            reason = exception.reason
        elif exception.exception_type == ScheduleExceptionType.SPECIAL_HOURS:
            opens_at, closes_at = exception.opens_at, exception.closes_at
        elif exception.exception_type == ScheduleExceptionType.REDUCED_CAPACITY:
            capacity_factor = exception.capacity_factor

    if opens_at is None and schedule is not None:
        if schedule.is_closed:
            if state is None:
                state = AvailabilityState.CLOSED
                reason = reason or "Closed on this weekday"
        else:
            opens_at, closes_at = schedule.open_time, schedule.close_time

    if state is None:
        if schedule is None:
            if await _any_schedule_published(db, organization.id, scope_ids):
                # Calendar is published but this weekday has no hours row
                state = AvailabilityState.NOT_OPEN
                reason = "No published operating hours for this date"
            elif (
                exception is not None
                and exception.exception_type == ScheduleExceptionType.SPECIAL_HOURS
            ):
                # Legacy tenant (no published schedule anywhere) with a
                # special-hours exception: honor the window instead of
                # silently discarding it (review defect D3).
                if exception.opens_at is None or exception.closes_at is None:
                    return DayAvailability(
                        state=AvailabilityState.NOT_OPEN,
                        reason="Special hours window not configured",
                        timezone_name=tz_name,
                        queue_mode=queue_mode,
                        is_legacy_day_queue=True,
                        exception=exception,
                    )
                return DayAvailability(
                    state=AvailabilityState.AVAILABLE,
                    reason=exception.reason,
                    opens_at=exception.opens_at,
                    closes_at=exception.closes_at,
                    timezone_name=tz_name,
                    queue_mode=queue_mode,
                    is_legacy_day_queue=True,
                    exception=exception,
                )
            else:
                # Legacy compatibility (OPEN_DECISIONS §4 design notes):
                # tenants with no published schedule keep Phase 1-2
                # day-queue behavior — bookable all day. Active
                # reduced-capacity exceptions still surface their factor
                # (review defect D3); closure exceptions never reach this
                # branch because they set `state` above.
                return DayAvailability(
                    state=AvailabilityState.AVAILABLE,
                    timezone_name=tz_name,
                    capacity_factor=(
                        exception.capacity_factor if exception is not None else None
                    ),
                    queue_mode=queue_mode,
                    is_legacy_day_queue=True,
                    exception=exception,
                )
        elif opens_at is None and closes_at is None:
            state = AvailabilityState.NOT_OPEN
            reason = reason or "Special hours window not configured"

    if state is None and opens_at is not None:
        state = AvailabilityState.AVAILABLE
        if capacity_factor is None and exception is not None:
            capacity_factor = exception.capacity_factor

    # Slot occupancy for slot-mode counters
    if (
        state in (AvailabilityState.AVAILABLE, AvailabilityState.LIMITED)
        and counter is not None
        and counter.queue_mode == QueueMode.SLOTS
    ):
        slots = await list_slots_for_date(db, counter.id, target_date)
        if slots:
            total = sum(s.capacity for s in slots)
            remaining = sum(max(s.capacity - s.booked_count - s.held_count, 0) for s in slots)
            if remaining == 0:
                state = AvailabilityState.FULL
            elif total > 0 and Decimal(remaining) / Decimal(total) < LIMITED_CAPACITY_RATIO:
                state = AvailabilityState.LIMITED

    slot_interval = None
    if counter is not None and counter.queue_mode == QueueMode.SLOTS:
        slot_interval = await _resolve_slot_interval(
            db, organization.id, day_of_week, scope_ids,
            schedule_overlay=schedule_overlay,
        )

    return DayAvailability(
        state=state,
        reason=reason,
        opens_at=opens_at,
        closes_at=closes_at,
        timezone_name=tz_name,
        capacity_factor=capacity_factor,
        slot_interval_minutes=slot_interval,
        queue_mode=queue_mode,
        exception=exception,
    )


# ── Capacity rules ────────────────────────────────────────────────────────────

async def get_slot_capacity(
    db: AsyncSession,
    organization_id: int,
    counter: Counter,
    capacity_factor: Optional[Decimal] = None,
    capacity_base: Optional[int] = None,
) -> int:
    """Base per-slot capacity from a counter-scope `slot_capacity` rule,
    else DEFAULT_SLOT_CAPACITY; reduced-capacity exceptions scale it down.

    `capacity_base` (SCH-007 impact preview): a proposed slot-capacity
    rule value to use instead of the database rule, so previews compute
    the would-be capacity without mutating configuration."""
    if capacity_base is None:
        stmt = select(CapacityRule).where(
            CapacityRule.organization_id == organization_id,
            CapacityRule.scope_level == ScheduleScopeLevel.COUNTER,
            CapacityRule.scope_id == counter.id,
            CapacityRule.limit_type == CapacityLimitType.SLOT_CAPACITY,
            CapacityRule.booking_source == "all",
            CapacityRule.is_active.is_(True),
        )
        rule = (await db.execute(stmt)).scalars().first()
        capacity_base = rule.max_count if rule is not None else DEFAULT_SLOT_CAPACITY
    if capacity_factor is not None:
        scaled = (Decimal(capacity_base) * Decimal(capacity_factor)).to_integral_value(
            rounding=ROUND_FLOOR
        )
        capacity_base = max(int(scaled), 1)
    return capacity_base


# ── Slot generation ───────────────────────────────────────────────────────────

async def list_slots_for_date(
    db: AsyncSession, counter_id: int, target_date: date
) -> list[TimeSlot]:
    stmt = (
        select(TimeSlot)
        .where(TimeSlot.counter_id == counter_id, TimeSlot.slot_date == target_date)
        .order_by(TimeSlot.start_time)
    )
    return list((await db.execute(stmt)).scalars().all())


async def generate_slots_for_date(
    db: AsyncSession,
    counter: Counter,
    target_date: date,
    organization: Optional[Organization] = None,
    branch: Optional[Branch] = None,
) -> list[TimeSlot]:
    """Idempotently create appointment slots for one date on a slot-mode
    counter. Missing start times are added; existing slots are never
    modified or duplicated (OPEN_DECISIONS §4 — atomic booking itself is
    Task 3). Returns the newly created slots."""
    if counter.queue_mode != QueueMode.SLOTS:
        return []

    organization = organization or await db.get(Organization, counter.organization_id)
    if branch is None and counter.branch_id is not None:
        branch = await db.get(Branch, counter.branch_id)

    availability = await resolve_day_availability(
        db, organization, target_date, branch=branch, counter=counter
    )
    if availability.state not in (
        AvailabilityState.AVAILABLE,
        AvailabilityState.LIMITED,
        AvailabilityState.FULL,
    ):
        return []
    if availability.opens_at is None or availability.closes_at is None:
        return []

    interval = availability.slot_interval_minutes
    if not interval or interval <= 0:
        return []

    existing = {
        slot.start_time for slot in await list_slots_for_date(db, counter.id, target_date)
    }

    capacity = await get_slot_capacity(
        db, counter.organization_id, counter, availability.capacity_factor
    )

    tz_name = availability.timezone_name

    created: list[TimeSlot] = []
    cursor = datetime.combine(target_date, availability.opens_at)
    close_dt = datetime.combine(target_date, availability.closes_at)
    while True:
        end = cursor + timedelta(minutes=interval)
        if end > close_dt:
            break
        # Times are stored in UTC (Decision 12); slot_date remains the
        # business-local date. A boundary whose UTC conversion lands on a
        # different calendar date cannot be represented by the stored pair
        # and fails loudly instead of producing wrong instants (defect D1).
        start_utc = utc_slot_time(target_date, cursor.time(), tz_name)
        end_utc = utc_slot_time(target_date, end.time(), tz_name)
        if start_utc is None or end_utc is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Operating hours on {target_date} in timezone {tz_name!r} "
                    "cross a UTC date boundary; slot times around "
                    f"{cursor.time():%H:%M}-{end.time():%H:%M} local cannot be "
                    "stored in UTC. Adjust the schedule or timezone so all "
                    "operating hours map to the same UTC calendar date."
                ),
            )
        if start_utc not in existing:
            slot = TimeSlot(
                organization_id=counter.organization_id,
                counter_id=counter.id,
                service_id=counter.service_id,
                slot_date=target_date,
                start_time=start_utc,
                end_time=end_utc,
                capacity=capacity,
            )
            db.add(slot)
            created.append(slot)
        cursor = end

    if created:
        await db.flush()
    return created


async def ensure_slots_for_date(
    db: AsyncSession,
    counter: Counter,
    target_date: date,
    organization: Optional[Organization] = None,
    branch: Optional[Branch] = None,
) -> list[TimeSlot]:
    """Lazily generate slots for a specific requested date. Enforces the
    future-booking horizon; past dates are returned as-is (no generation)."""
    today_local = datetime.now(_zone(get_timezone_name(
        organization or await db.get(Organization, counter.organization_id),
        branch,
    ))).date()
    days_ahead = (target_date - today_local).days
    if days_ahead > MAX_ADVANCE_BOOKING_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Bookings are only available up to {MAX_ADVANCE_BOOKING_DAYS} days in advance"
            ),
        )
    if days_ahead < 0:
        return []
    return await generate_slots_for_date(
        db, counter, target_date, organization=organization, branch=branch
    )


# ── Published availability calendar (SCH-001) ─────────────────────────────────

async def get_published_availability(
    db: AsyncSession,
    organization: Organization,
    start_date: date,
    days: int = PUBLISH_HORIZON_DAYS,
    branch: Optional[Branch] = None,
    counter: Optional[Counter] = None,
    service: Optional[Service] = None,
) -> list[DayAvailability]:
    """Build the user-visible availability calendar starting at `start_date`
    for `days` days (default 31 — at least one month).

    Slot-mode counters get idempotent slot generation for each listed date;
    past dates in the requested window are resolved but never generate
    slots (review defect D2 — matches `ensure_slots_for_date`); dates
    beyond PUBLISH_HORIZON_DAYS are NOT generated here — they are
    generated safely on demand via `ensure_slots_for_date`."""
    if days < 1 or days > MAX_ADVANCE_BOOKING_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Availability window must be between 1 and "
                f"{MAX_ADVANCE_BOOKING_DAYS} days"
            ),
        )

    tz_name = get_timezone_name(organization, branch)
    today_local = datetime.now(_zone(tz_name)).date()

    results: list[DayAvailability] = []
    for offset in range(days):
        target_date = start_date + timedelta(days=offset)
        if (
            counter is not None
            and counter.queue_mode == QueueMode.SLOTS
            and target_date >= today_local
        ):
            await generate_slots_for_date(
                db, counter, target_date, organization=organization, branch=branch
            )
        results.append(
            await resolve_day_availability(
                db, organization, target_date,
                branch=branch, counter=counter, service=service,
            )
        )
    return results


# ── Public slot-date discovery (Decision 15, SRCH-001/002) ────────────────────

@dataclass
class SlotDateSummary:
    """One business date's public bookability summary for an organization."""

    date: date
    state: AvailabilityState
    remaining: Optional[int]            # aggregate free places across active slot-mode counters; None = no slot inventory (legacy day-queue)
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    timezone_name: str = DEFAULT_TIMEZONE
    is_legacy_day_queue: bool = False


async def get_public_slot_dates(
    db: AsyncSession,
    organization: Organization,
    days: int = PUBLISH_HORIZON_DAYS,
    branch: Optional[Branch] = None,
) -> list[SlotDateSummary]:
    """Public discovery view: per-date bookability over the booking horizon.

    For every date in `[tenant-today, tenant-today + days]` (capped at
    MAX_ADVANCE_BOOKING_DAYS) this lazily generates slots for each active
    slot-mode counter (same idempotent generator the booking flow uses),
    aggregates remaining capacity across those counters, and resolves the
    day state (closures/holidays/unpublished hours via SCH-008 rules).
    Same-day cutoff (SCH-005): today's remaining counts only slots whose
    start instant is still ahead — a day with slots but nothing bookable
    left shows remaining 0 (FULL), not legacy "no inventory". Legacy tenants
    without published schedules surface as day-queue AVAILABLE dates with
    `remaining = None`.
    """
    if days < 1 or days > MAX_ADVANCE_BOOKING_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"days must be between 1 and {MAX_ADVANCE_BOOKING_DAYS}",
        )

    tz_name = get_timezone_name(organization, branch)
    today_local = get_tenant_today(organization, branch)

    counters = list(
        (
            await db.execute(
                select(Counter).where(
                    Counter.organization_id == organization.id,
                    Counter.queue_mode == QueueMode.SLOTS,
                    Counter.status == CounterStatus.ACTIVE,
                )
            )
        ).scalars().all()
    )

    # Idempotent slot generation for the whole horizon, per slot-mode counter
    for counter in counters:
        for offset in range(days):
            await generate_slots_for_date(
                db, counter, today_local + timedelta(days=offset),
                organization=organization, branch=branch,
            )

    # Aggregate remaining capacity per date (CHECK enforces booked+held<=capacity,
    # so remaining = capacity_sum - occupancy_sum, clamped at 0)
    # Same-day cutoff consistency (SCH-005): start instants that have already
    # passed are no longer bookable (token_service._validate_and_reserve_slot),
    # so today's summary counts only slots that still lie ahead — stored as
    # (slot_date, UTC time-of-day), a slot of today is elapsed exactly when
    # its UTC start time-of-day is ≤ now's UTC time-of-day. The unfiltered
    # aggregate is kept to distinguish "no inventoried slots at all today"
    # (legacy/None) from "everything today has started or filled" (Full/0).
    now_utc = _utcnow()
    horizon_end = today_local + timedelta(days=days)

    async def _aggregate_capacity(require_bookable: bool):
        where_clauses = [
            TimeSlot.organization_id == organization.id,
            TimeSlot.is_active.is_(True),
            TimeSlot.slot_date >= today_local,
            TimeSlot.slot_date < horizon_end,
            Counter.queue_mode == QueueMode.SLOTS,
            Counter.status == CounterStatus.ACTIVE,
        ]
        if require_bookable:
            where_clauses.append(
                or_(
                    TimeSlot.slot_date > today_local,
                    and_(
                        TimeSlot.slot_date == today_local,
                        TimeSlot.start_time > now_utc.time(),
                    ),
                )
            )
        rows = (
            await db.execute(
                select(
                    TimeSlot.slot_date,
                    func.sum(TimeSlot.capacity),
                    func.sum(TimeSlot.booked_count + TimeSlot.held_count),
                )
                .join(Counter, Counter.id == TimeSlot.counter_id)
                .where(*where_clauses)
                .group_by(TimeSlot.slot_date)
            )
        ).all()
        return {
            row[0]: (int(row[1] or 0), int(row[2] or 0)) for row in rows
        }

    capacity_by_date = await _aggregate_capacity(require_bookable=False)
    bookable_by_date = await _aggregate_capacity(require_bookable=True)

    summaries: list[SlotDateSummary] = []
    for offset in range(days):
        target_date = today_local + timedelta(days=offset)
        availability = await resolve_day_availability(
            db, organization, target_date, branch=branch
        )
        state = availability.state
        remaining: Optional[int] = None
        if target_date in bookable_by_date:
            cap_sum, occupied_sum = bookable_by_date[target_date]
            remaining = max(cap_sum - occupied_sum, 0)
        elif (
            target_date == today_local
            and target_date in capacity_by_date
        ):
            # Same-day cutoff: every inventoried place of today has either
            # started or been consumed — nothing remains bookable today.
            remaining = 0
        if remaining is not None:
            cap_ref = bookable_by_date.get(target_date, (0, 0))[0]
            if state in (AvailabilityState.AVAILABLE, AvailabilityState.LIMITED):
                if remaining == 0:
                    state = AvailabilityState.FULL
                elif (
                    cap_ref > 0
                    and Decimal(remaining) / Decimal(cap_ref) < LIMITED_CAPACITY_RATIO
                ):
                    state = AvailabilityState.LIMITED
        summaries.append(
            SlotDateSummary(
                date=target_date,
                state=state,
                remaining=remaining,
                opens_at=availability.opens_at,
                closes_at=availability.closes_at,
                timezone_name=tz_name,
                is_legacy_day_queue=availability.is_legacy_day_queue,
            )
        )
    return summaries


@dataclass
class PublicSlotInfo:
    """One bookable occurrence rendered in the tenant timezone (SCH-009)."""

    id: int
    counter_id: int
    counter_number: int
    counter_name: str
    slot_date: date
    start_local: time
    end_local: time
    start_utc: datetime
    end_utc: datetime
    capacity: int
    remaining: int
    is_bookable: bool


@dataclass
class PublicSlotDay:
    """One business date's public slot ladder for an organization."""

    date: date
    state: AvailabilityState
    reason: Optional[str] = None
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    timezone_name: str = DEFAULT_TIMEZONE
    is_legacy_day_queue: bool = False
    slots: list[PublicSlotInfo] = None  # type: ignore[assignment]


async def get_public_slots_for_date(
    db: AsyncSession,
    organization: Organization,
    target_date: date,
    branch: Optional[Branch] = None,
    counter_id: Optional[int] = None,
) -> PublicSlotDay:
    """Public slot ladder for ONE business date (SCH-005/SCH-009): the
    per-slot complement of `get_public_slot_dates`.

    Renders every active slot of the date in the tenant timezone (branch
    override wins, Decision 12): the stored (slot_date, UTC time) pair is
    mapped back to the local wall clock, and the absolute UTC instants are
    included for machine consumers. Slots whose start instant has already
    passed (same-day cutoff, SCH-005) are excluded — the booking engine
    can never accept them, so the calendar never advertises them — while
    filled-but-unstarted slots stay visible with remaining = 0 and
    `is_bookable = False` (live-capacity transparency). Non-bookable day
    states (closed/holiday/NOT_OPEN/service-unavailable) return the state
    with an empty list; legacy day-queue tenants (no published schedule)
    surface `is_legacy_day_queue` so clients fall back to slot-less
    booking. This is a pure read/render view: no tokens are touched and
    no capacity semantics beyond cap − booked − held are implied.
    """
    today_local = get_tenant_today(organization, branch)
    if target_date < today_local or target_date > today_local + timedelta(
        days=MAX_ADVANCE_BOOKING_DAYS
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"date must be between {today_local.isoformat()} and "
                f"{(today_local + timedelta(days=MAX_ADVANCE_BOOKING_DAYS)).isoformat()} "
                "in the organization timezone"
            ),
        )

    tenant_counter: Optional[Counter] = None
    if counter_id is not None:
        tenant_counter = await db.get(Counter, counter_id)
        if (
            tenant_counter is None
            or tenant_counter.organization_id != organization.id
            or tenant_counter.status != CounterStatus.ACTIVE
            or tenant_counter.queue_mode != QueueMode.SLOTS
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Counter not found"
            )

    availability = await resolve_day_availability(
        db, organization, target_date, branch=branch
    )
    state = availability.state
    tz_name = availability.timezone_name
    slots: list[TimeSlot] = []

    if state in (AvailabilityState.AVAILABLE, AvailabilityState.LIMITED):
        counter_clauses = [
            Counter.organization_id == organization.id,
            Counter.queue_mode == QueueMode.SLOTS,
            Counter.status == CounterStatus.ACTIVE,
        ]
        if branch is not None:
            counter_clauses.append(Counter.branch_id == branch.id)
        if tenant_counter is not None:
            counter_clauses.append(Counter.id == tenant_counter.id)
        counters = list(
            (await db.execute(select(Counter).where(*counter_clauses)))
            .scalars()
            .all()
        )

        # Idempotent lazy generation for the requested date (same generator
        # the booking flow and calendar view use).
        for counter in counters:
            await generate_slots_for_date(
                db, counter, target_date,
                organization=organization, branch=branch,
            )

        if counters:
            # Same-day cutoff consistency (SCH-005): started slots are never
            # bookable, so today's ladder lists only slots ahead of now.
            now_utc = _utcnow()
            where_clauses = [
                TimeSlot.organization_id == organization.id,
                TimeSlot.is_active.is_(True),
                TimeSlot.slot_date == target_date,
                Counter.id.in_([c.id for c in counters]),
            ]
            if target_date == today_local:
                where_clauses.append(TimeSlot.start_time > now_utc.time())
            rows = (
                await db.execute(
                    select(TimeSlot, Counter)
                    .join(Counter, Counter.id == TimeSlot.counter_id)
                    .where(*where_clauses)
                    .order_by(TimeSlot.start_time, Counter.id)
                )
            ).all()
            infos = []
            for slot, counter in rows:
                remaining = max(
                    slot.capacity - slot.booked_count - slot.held_count, 0
                )
                infos.append(
                    PublicSlotInfo(
                        id=int(slot.id),
                        counter_id=int(counter.id),
                        counter_number=int(counter.counter_number),
                        counter_name=counter.counter_name,
                        slot_date=slot.slot_date,
                        start_local=utc_pair_to_local_time(
                            slot.slot_date, slot.start_time, tz_name
                        ),
                        end_local=utc_pair_to_local_time(
                            slot.slot_date, slot.end_time, tz_name
                        ),
                        start_utc=utc_pair_to_utc_datetime(
                            slot.slot_date, slot.start_time
                        ),
                        end_utc=utc_pair_to_utc_datetime(
                            slot.slot_date, slot.end_time
                        ),
                        capacity=int(slot.capacity),
                        remaining=remaining,
                        is_bookable=remaining > 0,
                    )
                )
            slots = infos  # type: ignore[assignment]

            # Mirror the aggregate state adjustment of `get_public_slot_dates`:
            # after the per-slot cutoff filter nothing bookable → FULL today.
            if state == AvailabilityState.AVAILABLE and infos:
                remaining_sum = sum(s.remaining for s in infos)
                if remaining_sum == 0:
                    state = AvailabilityState.FULL
                else:
                    total = sum(s.capacity for s in infos)
                    if total > 0 and (
                        Decimal(remaining_sum) / Decimal(total)
                        < LIMITED_CAPACITY_RATIO
                    ):
                        state = AvailabilityState.LIMITED

    return PublicSlotDay(
        date=target_date,
        state=state,
        reason=availability.reason,
        opens_at=availability.opens_at,
        closes_at=availability.closes_at,
        timezone_name=tz_name,
        is_legacy_day_queue=availability.is_legacy_day_queue,
        slots=slots,  # type: ignore[arg-type]
    )


# ── Schedule administration (Phase 3 — org-panel CRUD APIs) ──────────────────
#
# CRUD for weekly schedules, schedule exceptions and capacity rules.
# Deliberately separated from the resolution/generation engine above: these
# functions ONLY write configuration rows. They never create, modify or
# delete generated `time_slots` — already-generated slots (and any bookings
# or holds inside them) are preserved untouched; cleanup/regeneration of
# affected slots belongs to the Decision 11 schedule-change impact workflow
# (SCH-007, not yet implemented — see OPEN_DECISIONS "Decision 17").

async def validate_scope_target(
    db: AsyncSession,
    organization_id: int,
    scope_level: ScheduleScopeLevel,
    scope_id: int,
) -> None:
    """Validate that a schedule scope target exists and belongs to the tenant.

    - org scope: scope_id MUST be the caller's own organization id.
    - branch scope: active branch owned by the organization.
    - counter scope: ACTIVE counter owned by the organization.
    - service scope: active service owned by the organization.
    Cross-tenant or unknown targets raise ValueError ("not found" so routers
    map to 404 without leaking other tenants' ids).
    """
    if scope_id < 1:
        raise ValueError("Scope target not found")
    if scope_level == ScheduleScopeLevel.ORGANIZATION:
        if scope_id != organization_id:
            raise ValueError("Organization-scoped schedules must use this organization's id")
        return
    if scope_level == ScheduleScopeLevel.BRANCH:
        branch = await db.get(Branch, scope_id)
        if branch is None or branch.organization_id != organization_id:
            raise ValueError("Branch not found")
        if not branch.is_active:
            raise ValueError("Branch is not active")
    elif scope_level == ScheduleScopeLevel.COUNTER:
        counter = await db.get(Counter, scope_id)
        if counter is None or counter.organization_id != organization_id:
            raise ValueError("Counter not found")
        if counter.status != CounterStatus.ACTIVE:
            raise ValueError("Counter is not active")
    elif scope_level == ScheduleScopeLevel.SERVICE:
        service = await db.get(Service, scope_id)
        if service is None or service.organization_id != organization_id:
            raise ValueError("Service not found")
        if not service.is_active:
            raise ValueError("Service is not active")
    else:
        raise ValueError("Invalid scope level")


def _validate_hours(open_time: Optional[time], close_time: Optional[time]) -> None:
    if open_time is None or close_time is None:
        raise ValueError("Operating hours are required unless the day is marked closed")
    if open_time >= close_time:
        raise ValueError("open_time must be earlier than close_time")


# ── Weekly schedule CRUD ──────────────────────────────────────────────────────
#
# Each CRUD op is split into a commit-free validation/apply core and a thin
# committing wrapper. Organization-level callers (the API) use the wrappers;
# the SCH-007 impact workflow reuses the cores so confirmation applies the
# same validated mutation inside its own single transaction (Decision 11),
# while ordinary CRUD keeps Decision 17 behavior (slots/bookings untouched).

async def get_weekly_schedule(
    db: AsyncSession, organization_id: int, schedule_id: int
) -> WeeklySchedule:
    """Tenant-checked fetch; unknown or foreign ids are indistinguishable."""
    row = await db.get(WeeklySchedule, schedule_id)
    if row is None or row.organization_id != organization_id:
        raise ValueError("Weekly schedule not found")
    return row


async def list_weekly_schedules(
    db: AsyncSession,
    organization_id: int,
    scope_level: Optional[ScheduleScopeLevel] = None,
    scope_id: Optional[int] = None,
) -> list[WeeklySchedule]:
    stmt = select(WeeklySchedule).where(WeeklySchedule.organization_id == organization_id)
    if scope_level is not None:
        stmt = stmt.where(WeeklySchedule.scope_level == scope_level)
    if scope_id is not None:
        stmt = stmt.where(WeeklySchedule.scope_id == scope_id)
    stmt = stmt.order_by(WeeklySchedule.day_of_week)
    return list((await db.execute(stmt)).scalars().all())


async def _new_weekly_values(db: AsyncSession, organization_id: int, data: dict) -> dict:
    """Validate a proposed new weekly-schedule row and return its DB fields.
    Reads only — raises ValueError on any invalid input (including a
    conflicting existing row)."""
    scope_level = ScheduleScopeLevel(data["scope_level"])
    scope_id = int(data["scope_id"])
    await validate_scope_target(db, organization_id, scope_level, scope_id)

    day_of_week = int(data["day_of_week"])
    if not 0 <= day_of_week <= 6:
        raise ValueError("day_of_week must be between 0 (Monday) and 6 (Sunday)")

    is_closed = bool(data.get("is_closed", False))
    open_time_val = data.get("open_time")
    close_time_val = data.get("close_time")
    interval = data.get("slot_interval_minutes")

    if is_closed:
        # Closed days carry no operating configuration (matches the model
        # CHECK `is_closed = 1 OR (open < close)`).
        open_time_val = close_time_val = None
        interval = None
    else:
        _validate_hours(open_time_val, close_time_val)
    if interval is not None and not 1 <= int(interval) <= 1440:
        raise ValueError("slot_interval_minutes must be between 1 and 1440")

    duplicate = (
        await db.execute(
            select(WeeklySchedule).where(
                WeeklySchedule.organization_id == organization_id,
                WeeklySchedule.scope_level == scope_level,
                WeeklySchedule.scope_id == scope_id,
                WeeklySchedule.day_of_week == day_of_week,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ValueError("A weekly schedule for this scope and weekday already exists")

    return {
        "scope_level": scope_level,
        "scope_id": scope_id,
        "day_of_week": day_of_week,
        "is_closed": is_closed,
        "open_time": open_time_val,
        "close_time": close_time_val,
        "slot_interval_minutes": interval,
    }


async def _create_weekly_core(
    db: AsyncSession, organization_id: int, data: dict
) -> WeeklySchedule:
    """Commit-free create (validation + INSERT + flush). Used by the CRUD
    wrapper and by the SCH-007 confirmation transaction."""
    vals = await _new_weekly_values(db, organization_id, data)
    row = WeeklySchedule(organization_id=organization_id, **vals)
    db.add(row)
    await db.flush()
    return row


async def create_weekly_schedule(
    db: AsyncSession, organization_id: int, data: dict
) -> WeeklySchedule:
    """Create one weekday row for one scope. Duplicate (scope, weekday) rows
    are rejected with a user-facing error before and after the INSERT."""
    row = await _create_weekly_core(db, organization_id, data)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError("A weekly schedule for this scope and weekday already exists")
    await db.refresh(row)
    return row


async def _prospective_weekly_values(
    db: AsyncSession, organization_id: int, row: WeeklySchedule, data: dict
) -> dict:
    """Partial-update merge WITHOUT mutating the row. Returns the prospective
    DB fields after re-validating scope ownership, hours, interval and the
    duplicate (scope, weekday) constraint — exactly the checks
    `update_weekly_schedule` performed inline before this split.
    (Autoflush-safe: no mutation happens first, so the duplicate-check
    SELECT cannot flush a conflicting intermediate state.)"""
    # 1. Compute prospective values WITHOUT mutating the row first: a
    # conflicting mutation would be autoflushed by the duplicate-check
    # SELECT below and hit the UNIQUE constraint as a raw IntegrityError.
    new_level = data.get("scope_level")
    new_scope_id = data.get("scope_id")
    if (new_level is None) != (new_scope_id is None):
        raise ValueError("scope_level and scope_id must be provided together")
    if new_level is not None:
        prospective_level = ScheduleScopeLevel(new_level)
        prospective_scope_id = int(new_scope_id)
        await validate_scope_target(db, organization_id, prospective_level, prospective_scope_id)
    else:
        prospective_level, prospective_scope_id = row.scope_level, row.scope_id

    if data.get("day_of_week") is not None:
        prospective_dow = int(data["day_of_week"])
        if not 0 <= prospective_dow <= 6:
            raise ValueError("day_of_week must be between 0 (Monday) and 6 (Sunday)")
    else:
        prospective_dow = row.day_of_week

    provided_open = data.get("open_time")
    provided_close = data.get("close_time")

    will_be_closed: Optional[bool] = data.get("is_closed")
    if will_be_closed is None and row.is_closed and (
        provided_open is not None or provided_close is not None
    ):
        # Hours supplied for a previously closed row → caller is reopening it.
        will_be_closed = False
    if will_be_closed is None:
        will_be_closed = row.is_closed

    if will_be_closed:
        eff_open = eff_close = None
        eff_interval = None
    else:
        eff_open = provided_open if provided_open is not None else row.open_time
        eff_close = provided_close if provided_close is not None else row.close_time
        _validate_hours(eff_open, eff_close)
        provided_interval = data.get("slot_interval_minutes")
        if provided_interval is not None:
            eff_interval = int(provided_interval)
            if not 1 <= eff_interval <= 1440:
                raise ValueError("slot_interval_minutes must be between 1 and 1440")
        else:
            eff_interval = row.slot_interval_minutes

    # 2. Duplicate guard for the prospective (scope, weekday) — excludes self.
    duplicate = (
        await db.execute(
            select(WeeklySchedule).where(
                WeeklySchedule.organization_id == organization_id,
                WeeklySchedule.scope_level == prospective_level,
                WeeklySchedule.scope_id == prospective_scope_id,
                WeeklySchedule.day_of_week == prospective_dow,
                WeeklySchedule.id != row.id,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ValueError("A weekly schedule for this scope and weekday already exists")

    return {
        "scope_level": prospective_level,
        "scope_id": prospective_scope_id,
        "day_of_week": prospective_dow,
        "is_closed": will_be_closed,
        "open_time": eff_open,
        "close_time": eff_close,
        "slot_interval_minutes": eff_interval,
    }


async def _update_weekly_core(
    db: AsyncSession, organization_id: int, schedule_id: int, data: dict
) -> WeeklySchedule:
    """Commit-free partial update (validation + apply + flush). Used by the
    CRUD wrapper and by the SCH-007 confirmation transaction."""
    row = await get_weekly_schedule(db, organization_id, schedule_id)
    vals = await _prospective_weekly_values(db, organization_id, row, data)
    for field, value in vals.items():
        setattr(row, field, value)
    await db.flush()
    return row


async def update_weekly_schedule(
    db: AsyncSession, organization_id: int, schedule_id: int, data: dict
) -> WeeklySchedule:
    """Partial update. Scope moves re-validate ownership. Setting
    `is_closed = True` clears hours and slot interval (model CHECK); turning
    `is_closed` off requires effective operating hours."""
    try:
        row = await _update_weekly_core(db, organization_id, schedule_id, data)
    except IntegrityError:
        await db.rollback()
        raise ValueError("A weekly schedule for this scope and weekday already exists")
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError("A weekly schedule for this scope and weekday already exists")
    await db.refresh(row)
    return row


async def _delete_weekly_core(db: AsyncSession, organization_id: int, schedule_id: int):
    """Commit-free delete of a configuration row. Already-generated
    `time_slots` (and any bookings inside them) are deliberately NOT
    touched — see the section docstring and OPEN_DECISIONS "Decision 17"."""
    row = await get_weekly_schedule(db, organization_id, schedule_id)
    await db.delete(row)
    await db.flush()
    return row


async def delete_weekly_schedule(
    db: AsyncSession, organization_id: int, schedule_id: int
) -> None:
    """Delete a configuration row only. Already-generated `time_slots` (and
    any bookings inside them) are deliberately NOT touched — see the section
    docstring and OPEN_DECISIONS "Decision 17"."""
    await _delete_weekly_core(db, organization_id, schedule_id)
    await db.commit()


# ── Schedule exception CRUD ───────────────────────────────────────────────────

def _validate_exception_fields(
    exception_type: ScheduleExceptionType,
    opens_at: Optional[time],
    closes_at: Optional[time],
    capacity_factor: Optional[Decimal],
) -> None:
    if exception_type == ScheduleExceptionType.SPECIAL_HOURS:
        if opens_at is None or closes_at is None:
            raise ValueError("Special-hours exceptions require opens_at and closes_at")
        if opens_at >= closes_at:
            raise ValueError("Special-hours window must open before it closes")
        if capacity_factor is not None:
            raise ValueError("capacity_factor applies only to reduced_capacity exceptions")
    elif exception_type == ScheduleExceptionType.REDUCED_CAPACITY:
        if capacity_factor is None:
            raise ValueError("Reduced-capacity exceptions require a capacity_factor")
        if not (Decimal("0") < capacity_factor <= Decimal("1")):
            raise ValueError("capacity_factor must be greater than 0 and at most 1")
        if opens_at is not None or closes_at is not None:
            raise ValueError("Operating-window overrides use special_hours, not reduced_capacity")
    else:
        # Closure types (holiday/emergency/maintenance/counter/service)
        if opens_at is not None or closes_at is not None or capacity_factor is not None:
            raise ValueError("Closure exceptions carry only a reason — no hours or capacity factor")


async def get_schedule_exception(
    db: AsyncSession, organization_id: int, exception_id: int
) -> ScheduleException:
    row = await db.get(ScheduleException, exception_id)
    if row is None or row.organization_id != organization_id:
        raise ValueError("Schedule exception not found")
    return row


async def list_schedule_exceptions(
    db: AsyncSession,
    organization_id: int,
    scope_level: Optional[ScheduleScopeLevel] = None,
    scope_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> list[ScheduleException]:
    stmt = select(ScheduleException).where(
        ScheduleException.organization_id == organization_id
    )
    if scope_level is not None:
        stmt = stmt.where(ScheduleException.scope_level == scope_level)
    if scope_id is not None:
        stmt = stmt.where(ScheduleException.scope_id == scope_id)
    if date_from is not None:
        stmt = stmt.where(ScheduleException.exception_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(ScheduleException.exception_date <= date_to)
    stmt = stmt.order_by(ScheduleException.exception_date)
    return list((await db.execute(stmt)).scalars().all())


async def _new_exception_values(
    db: AsyncSession, organization_id: int, data: dict
) -> dict:
    """Validate a proposed new schedule-exception row and return its DB
    fields (commit-free; duplicate conflict raises ValueError)."""
    scope_level = ScheduleScopeLevel(data["scope_level"])
    scope_id = int(data["scope_id"])
    await validate_scope_target(db, organization_id, scope_level, scope_id)

    exception_type = ScheduleExceptionType(data["exception_type"])
    opens_at = data.get("opens_at")
    closes_at = data.get("closes_at")
    capacity_factor = data.get("capacity_factor")
    _validate_exception_fields(exception_type, opens_at, closes_at, capacity_factor)

    exception_date = data["exception_date"]
    duplicate = (
        await db.execute(
            select(ScheduleException).where(
                ScheduleException.organization_id == organization_id,
                ScheduleException.scope_level == scope_level,
                ScheduleException.scope_id == scope_id,
                ScheduleException.exception_date == exception_date,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ValueError("A schedule exception for this scope and date already exists")

    return {
        "scope_level": scope_level,
        "scope_id": scope_id,
        "exception_date": exception_date,
        "exception_type": exception_type,
        "reason": data["reason"],
        "opens_at": opens_at,
        "closes_at": closes_at,
        "capacity_factor": capacity_factor,
        "is_active": bool(data.get("is_active", True)),
    }


async def _create_exception_core(
    db: AsyncSession, organization_id: int, data: dict
) -> ScheduleException:
    """Commit-free create (validation + INSERT + flush). Used by the CRUD
    wrapper and by the SCH-007 confirmation transaction."""
    vals = await _new_exception_values(db, organization_id, data)
    row = ScheduleException(organization_id=organization_id, **vals)
    db.add(row)
    await db.flush()
    return row


async def create_schedule_exception(
    db: AsyncSession, organization_id: int, data: dict
) -> ScheduleException:
    row = await _create_exception_core(db, organization_id, data)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError("A schedule exception for this scope and date already exists")
    await db.refresh(row)
    return row


async def _prospective_exception_values(
    db: AsyncSession, organization_id: int, row: ScheduleException, data: dict
) -> dict:
    """Partial-update merge WITHOUT mutating the row (autoflush-safe, see
    `_prospective_weekly_values`); re-validates the exception-type field
    rules and the duplicate (scope, date) constraint, excluding self."""
    # Compute prospective values without mutating (autoflush-safe).
    if data.get("exception_type") is not None:
        prospective_type = ScheduleExceptionType(data["exception_type"])
    else:
        prospective_type = ScheduleExceptionType(row.exception_type)
    prospective_date = data.get("exception_date") or row.exception_date
    prospective_open = data.get("opens_at") or row.opens_at
    prospective_close = data.get("closes_at") or row.closes_at
    prospective_factor = data.get("capacity_factor") or row.capacity_factor
    _validate_exception_fields(
        prospective_type, prospective_open, prospective_close, prospective_factor
    )

    duplicate = (
        await db.execute(
            select(ScheduleException).where(
                ScheduleException.organization_id == organization_id,
                ScheduleException.scope_level == row.scope_level,
                ScheduleException.scope_id == row.scope_id,
                ScheduleException.exception_date == prospective_date,
                ScheduleException.id != row.id,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ValueError("A schedule exception for this scope and date already exists")

    return {
        "exception_type": prospective_type,
        "exception_date": prospective_date,
        "reason": data.get("reason") or row.reason,
        "opens_at": prospective_open,
        "closes_at": prospective_close,
        "capacity_factor": prospective_factor,
        "is_active": bool(data["is_active"]) if data.get("is_active") is not None else row.is_active,
    }


async def _update_exception_core(
    db: AsyncSession, organization_id: int, exception_id: int, data: dict
) -> ScheduleException:
    """Commit-free partial update. Used by the CRUD wrapper and by the
    SCH-007 confirmation transaction."""
    row = await get_schedule_exception(db, organization_id, exception_id)
    vals = await _prospective_exception_values(db, organization_id, row, data)
    for field, value in vals.items():
        setattr(row, field, value)
    await db.flush()
    return row


async def update_schedule_exception(
    db: AsyncSession, organization_id: int, exception_id: int, data: dict
) -> ScheduleException:
    row = await _update_exception_core(db, organization_id, exception_id, data)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError("A schedule exception for this scope and date already exists")
    await db.refresh(row)
    return row


async def _delete_exception_core(
    db: AsyncSession, organization_id: int, exception_id: int
):
    """Commit-free delete (Decision 17: generated slots/bookings untouched)."""
    row = await get_schedule_exception(db, organization_id, exception_id)
    await db.delete(row)
    await db.flush()
    return row


async def delete_schedule_exception(
    db: AsyncSession, organization_id: int, exception_id: int
) -> None:
    row = await _delete_exception_core(db, organization_id, exception_id)
    await db.commit()


# ── Capacity rule CRUD ────────────────────────────────────────────────────────

async def get_capacity_rule(
    db: AsyncSession, organization_id: int, rule_id: int
) -> CapacityRule:
    row = await db.get(CapacityRule, rule_id)
    if row is None or row.organization_id != organization_id:
        raise ValueError("Capacity rule not found")
    return row


async def list_capacity_rules(
    db: AsyncSession,
    organization_id: int,
    scope_level: Optional[ScheduleScopeLevel] = None,
    scope_id: Optional[int] = None,
    limit_type: Optional[CapacityLimitType] = None,
) -> list[CapacityRule]:
    stmt = select(CapacityRule).where(CapacityRule.organization_id == organization_id)
    if scope_level is not None:
        stmt = stmt.where(CapacityRule.scope_level == scope_level)
    if scope_id is not None:
        stmt = stmt.where(CapacityRule.scope_id == scope_id)
    if limit_type is not None:
        stmt = stmt.where(CapacityRule.limit_type == limit_type)
    stmt = stmt.order_by(CapacityRule.id)
    return list((await db.execute(stmt)).scalars().all())


async def _new_capacity_values(db: AsyncSession, organization_id: int, data: dict) -> dict:
    """Validate a proposed new capacity rule and return its DB fields
    (commit-free; duplicate conflict raises ValueError)."""
    scope_level = ScheduleScopeLevel(data["scope_level"])
    scope_id = int(data["scope_id"])
    await validate_scope_target(db, organization_id, scope_level, scope_id)

    limit_type = CapacityLimitType(data["limit_type"])
    booking_source = BookingSource(data.get("booking_source", BookingSource.ALL.value))
    max_count = int(data["max_count"])
    if max_count < 0:
        raise ValueError("max_count must be 0 or greater")

    duplicate = (
        await db.execute(
            select(CapacityRule).where(
                CapacityRule.organization_id == organization_id,
                CapacityRule.scope_level == scope_level,
                CapacityRule.scope_id == scope_id,
                CapacityRule.limit_type == limit_type,
                CapacityRule.booking_source == booking_source,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ValueError(
            "A capacity rule for this scope, limit type and booking source already exists"
        )

    return {
        "scope_level": scope_level,
        "scope_id": scope_id,
        "limit_type": limit_type,
        "booking_source": booking_source,
        "max_count": max_count,
        "is_active": bool(data.get("is_active", True)),
    }


async def _create_capacity_core(
    db: AsyncSession, organization_id: int, data: dict
) -> CapacityRule:
    """Commit-free create. Used by the CRUD wrapper and by the SCH-007
    confirmation transaction."""
    vals = await _new_capacity_values(db, organization_id, data)
    row = CapacityRule(organization_id=organization_id, **vals)
    db.add(row)
    await db.flush()
    return row


async def create_capacity_rule(
    db: AsyncSession, organization_id: int, data: dict
) -> CapacityRule:
    row = await _create_capacity_core(db, organization_id, data)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError(
            "A capacity rule for this scope, limit type and booking source already exists"
        )
    await db.refresh(row)
    return row


async def _prospective_capacity_values(
    db: AsyncSession, organization_id: int, row: CapacityRule, data: dict
) -> dict:
    """Partial-update merge WITHOUT mutating the row (autoflush-safe);
    re-validates scope ownership, max_count and the duplicate key."""
    new_level = data.get("scope_level")
    new_scope_id = data.get("scope_id")
    if (new_level is None) != (new_scope_id is None):
        raise ValueError("scope_level and scope_id must be provided together")
    if new_level is not None:
        prospective_level = ScheduleScopeLevel(new_level)
        prospective_scope_id = int(new_scope_id)
        await validate_scope_target(db, organization_id, prospective_level, prospective_scope_id)
    else:
        prospective_level, prospective_scope_id = row.scope_level, row.scope_id

    if data.get("limit_type") is not None:
        prospective_type = CapacityLimitType(data["limit_type"])
    else:
        prospective_type = CapacityLimitType(row.limit_type)
    if data.get("booking_source") is not None:
        prospective_source = BookingSource(data["booking_source"])
    else:
        prospective_source = BookingSource(row.booking_source)

    if data.get("max_count") is not None:
        prospective_max = int(data["max_count"])
        if prospective_max < 0:
            raise ValueError("max_count must be 0 or greater")
    else:
        prospective_max = row.max_count

    duplicate = (
        await db.execute(
            select(CapacityRule).where(
                CapacityRule.organization_id == organization_id,
                CapacityRule.scope_level == prospective_level,
                CapacityRule.scope_id == prospective_scope_id,
                CapacityRule.limit_type == prospective_type,
                CapacityRule.booking_source == prospective_source,
                CapacityRule.id != row.id,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ValueError(
            "A capacity rule for this scope, limit type and booking source already exists"
        )

    return {
        "scope_level": prospective_level,
        "scope_id": prospective_scope_id,
        "limit_type": prospective_type,
        "booking_source": prospective_source,
        "max_count": prospective_max,
        "is_active": bool(data["is_active"]) if data.get("is_active") is not None else row.is_active,
    }


async def _update_capacity_core(
    db: AsyncSession, organization_id: int, rule_id: int, data: dict
) -> CapacityRule:
    """Commit-free partial update. Used by the CRUD wrapper and by the
    SCH-007 confirmation transaction."""
    row = await get_capacity_rule(db, organization_id, rule_id)
    vals = await _prospective_capacity_values(db, organization_id, row, data)
    for field, value in vals.items():
        setattr(row, field, value)
    await db.flush()
    return row


async def update_capacity_rule(
    db: AsyncSession, organization_id: int, rule_id: int, data: dict
) -> CapacityRule:
    row = await _update_capacity_core(db, organization_id, rule_id, data)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError(
            "A capacity rule for this scope, limit type and booking source already exists"
        )
    await db.refresh(row)
    return row


async def _delete_capacity_core(db: AsyncSession, organization_id: int, rule_id: int):
    """Commit-free delete (Decision 17: generated slots/bookings untouched)."""
    row = await get_capacity_rule(db, organization_id, rule_id)
    await db.delete(row)
    await db.flush()
    return row


async def delete_capacity_rule(
    db: AsyncSession, organization_id: int, rule_id: int
) -> None:
    await _delete_capacity_core(db, organization_id, rule_id)
    await db.commit()
