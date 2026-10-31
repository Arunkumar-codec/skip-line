"""tests/unit/test_schedule_service.py — Phase 3 Task 2: schedule
resolution, exceptions, timezone handling and idempotent slot generation."""

import asyncio
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.services import schedule_service as svc
from app.services.schedule_service import (
    AvailabilityState,
    DEFAULT_TIMEZONE,
    get_published_availability,
    get_slot_capacity,
    generate_slots_for_date,
    ensure_slots_for_date,
    list_slots_for_date,
    local_to_utc_datetime,
    resolve_day_availability,
)
from models.branch import Branch
from models.capacity_rule import BookingSource, CapacityLimitType, CapacityRule
from models.counter import Counter, QueueMode
from models.organization import Organization
from models.schedule_exception import ScheduleException, ScheduleExceptionType
from models.service import Service
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule
from zoneinfo import ZoneInfo


# ── Fixture helpers ───────────────────────────────────────────────────────────

async def make_org(db, name="SchedSvc Org") -> Organization:
    org = Organization(
        name=name,
        registration_number=f"REG-{abs(hash(name)) % 10**8}",
        org_type="hospital",
        address="Test Address",
        city="Test City",
        state="Test State",
        email=f"{name.lower().replace(' ', '')}@test.example",
        mobile="9000000000",
        password_hash="x",
        status="approved",
    )
    db.add(org)
    await db.flush()
    return org


async def make_branch(db, org_id, code="M1", name="Main") -> Branch:
    branch = Branch(
        organization_id=org_id, name=name, branch_code=code,
        address="Addr", city="C", state="S", pincode="560001",
    )
    db.add(branch)
    await db.flush()
    return branch


async def make_counter(db, org, branch=None, mode=QueueMode.DAY_QUEUE) -> Counter:
    counter = Counter(
        organization_id=org.id,
        branch_id=branch.id if branch else None,
        counter_name=f"Counter {abs(hash(mode.value)) % 10**4}",
        counter_number=abs(hash(mode.value)) % 100 + 1,
        token_prefix="A",
        queue_mode=mode,
    )
    db.add(counter)
    await db.flush()
    return counter


async def make_service(db, org, name="Consultation") -> Service:
    svc_obj = Service(
        organization_id=org.id, name=name, price=Decimal("100.00"),
        estimated_minutes=10,
    )
    db.add(svc_obj)
    await db.flush()
    return svc_obj


async def add_schedule(db, org_id, scope_level, scope_id, dow, opens=None, closes=None,
                       closed=False, interval=None):
    row = WeeklySchedule(
        organization_id=org_id, scope_level=scope_level, scope_id=scope_id,
        day_of_week=dow, is_closed=closed, open_time=opens, close_time=closes,
        slot_interval_minutes=interval,
    )
    db.add(row)
    await db.flush()
    return row


def add_exception(db, org_id, scope_level, scope_id, on_date, exc_type, reason,
                  opens=None, closes=None, factor=None, active=True):
    row = ScheduleException(
        organization_id=org_id, scope_level=scope_level, scope_id=scope_id,
        exception_date=on_date, exception_type=exc_type, reason=reason,
        opens_at=opens, closes_at=closes, capacity_factor=factor,
        is_active=active,
    )
    db.add(row)
    return row


# 2026-10-28 is a Wednesday; 2026-11-02 is a Monday; 2026-11-01 a Sunday
MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)
WEDNESDAY = date(2026, 10, 28)
SUNDAY = date(2026, 11, 1)


class TestPrecedence:
    async def test_counter_overrides_organization(self, db_session):
        org = await make_org(db_session, "Prec Org")
        counter = await make_counter(db_session, org)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0))
        await add_schedule(db_session, org.id, ScheduleScopeLevel.COUNTER,
                           counter.id, 0, time(10, 0), time(18, 0))
        avail = await resolve_day_availability(db_session, org, MONDAY, counter=counter)
        assert avail.opens_at == time(10, 0)
        assert avail.closes_at == time(18, 0)

    async def test_service_overrides_counter(self, db_session):
        org = await make_org(db_session, "Prec Org 2")
        counter = await make_counter(db_session, org)
        service = await make_service(db_session, org, "Priority Svc")
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0))
        await add_schedule(db_session, org.id, ScheduleScopeLevel.COUNTER,
                           counter.id, 0, time(10, 0), time(18, 0))
        await add_schedule(db_session, org.id, ScheduleScopeLevel.SERVICE,
                           service.id, 0, time(11, 0), time(15, 0))
        avail = await resolve_day_availability(
            db_session, org, MONDAY, counter=counter, service=service
        )
        assert avail.opens_at == time(11, 0)

    async def test_branch_overrides_organization(self, db_session):
        org = await make_org(db_session, "Prec Org 3")
        branch = await make_branch(db_session, org.id)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0))
        await add_schedule(db_session, org.id, ScheduleScopeLevel.BRANCH,
                           branch.id, 0, time(8, 0), time(12, 0))
        avail = await resolve_day_availability(db_session, org, MONDAY, branch=branch)
        assert avail.opens_at == time(8, 0)

    async def test_counter_exception_wins_over_org_holiday(self, db_session):
        org = await make_org(db_session, "Prec Org 4")
        counter = await make_counter(db_session, org)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0))
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.HOLIDAY, "Org holiday")
        add_exception(
            db_session, org.id, ScheduleScopeLevel.COUNTER, counter.id, MONDAY,
            ScheduleExceptionType.SPECIAL_HOURS, "Counter open late",
            opens=time(14, 0), closes=time(20, 0),
        )
        avail = await resolve_day_availability(db_session, org, MONDAY, counter=counter)
        assert avail.state == AvailabilityState.AVAILABLE
        assert avail.opens_at == time(14, 0)
        assert avail.reason is None


class TestWeeklyResolution:
    async def test_published_calendar_missing_weekday_is_not_open(self, db_session):
        org = await make_org(db_session, "Published Org")
        counter = await make_counter(db_session, org)
        # Only Monday published
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0))
        avail = await resolve_day_availability(db_session, org, TUESDAY, counter=counter)
        assert avail.state == AvailabilityState.NOT_OPEN

    async def test_weekly_closed_day(self, db_session):
        org = await make_org(db_session, "Closed Day Org")
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 6, closed=True)
        avail = await resolve_day_availability(db_session, org, SUNDAY)
        assert avail.state == AvailabilityState.CLOSED

    async def test_legacy_day_queue_without_any_schedule(self, db_session):
        """No published schedule anywhere → Phase 1-2 day-queue behavior."""
        org = await make_org(db_session, "Legacy Org")
        counter = await make_counter(db_session, org)
        avail = await resolve_day_availability(db_session, org, MONDAY, counter=counter)
        assert avail.state == AvailabilityState.AVAILABLE
        assert avail.is_legacy_day_queue is True


class TestExceptions:
    async def test_holiday_state_and_reason(self, db_session):
        org = await make_org(db_session, "Holiday Org")
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0))
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.HOLIDAY,
                      "Diwali holiday")
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.state == AvailabilityState.HOLIDAY
        assert avail.reason == "Diwali holiday"

    async def test_emergency_closure(self, db_session):
        org = await make_org(db_session, "Emergency Org")
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0))
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.EMERGENCY_CLOSURE,
                      "Flash flood alert")
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.state == AvailabilityState.CLOSED
        assert avail.reason == "Flash flood alert"

    async def test_special_hours_overrides_window(self, db_session):
        org = await make_org(db_session, "Special Org")
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0))
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.SPECIAL_HOURS,
                      "Short day", opens=time(11, 0), closes=time(13, 0))
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.state == AvailabilityState.AVAILABLE
        assert (avail.opens_at, avail.closes_at) == (time(11, 0), time(13, 0))

    async def test_inactive_exception_ignored(self, db_session):
        org = await make_org(db_session, "Inactive Exc Org")
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0))
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.HOLIDAY,
                      "Retired holiday", active=False)
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.state == AvailabilityState.AVAILABLE


class TestTimezone:
    def test_local_to_utc_ist(self):
        utc = local_to_utc_datetime(MONDAY, time(9, 0), "Asia/Kolkata")
        assert utc.utcoffset() == timedelta(0)
        assert (utc.hour, utc.minute) == (3, 30)
        assert utc.date() == MONDAY

    def test_default_timezone_constant(self):
        assert DEFAULT_TIMEZONE == "Asia/Kolkata"

    async def test_organization_timezone_default(self, db_session):
        org = await make_org(db_session, "TZ Org")
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.timezone_name == "Asia/Kolkata"

    async def test_branch_timezone_override(self, db_session):
        org = await make_org(db_session, "TZ Override Org")
        branch = await make_branch(db_session, org.id)
        branch.timezone = "Asia/Dubai"
        avail = await resolve_day_availability(db_session, org, MONDAY, branch=branch)
        assert avail.timezone_name == "Asia/Dubai"

    async def test_branch_null_timezone_inherits_org(self, db_session):
        org = await make_org(db_session, "TZ Inherit Org")
        branch = await make_branch(db_session, org.id)
        org.timezone = "Asia/Dubai"
        avail = await resolve_day_availability(db_session, org, MONDAY, branch=branch)
        assert avail.timezone_name == "Asia/Dubai"

    def test_local_to_utc_dubai(self):
        utc = local_to_utc_datetime(MONDAY, time(9, 0), "Asia/Dubai")
        assert (utc.hour, utc.minute) == (5, 0)

    async def test_slots_stored_in_utc(self, db_session):
        org = await make_org(db_session, "Slot UTC Org")
        branch = await make_branch(db_session, org.id, code="IST")
        counter = await make_counter(db_session, org, branch, QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(11, 0), interval=60)
        await generate_slots_for_date(db_session, counter, MONDAY, organization=org)
        slots = await list_slots_for_date(db_session, counter.id, MONDAY)
        assert len(slots) == 2
        # 09:00 IST → 03:30 UTC; 11:00 IST → 05:30 UTC
        assert (slots[0].start_time.hour, slots[0].start_time.minute) == (3, 30)
        assert (slots[1].end_time.hour, slots[1].end_time.minute) == (5, 30)


class TestSlotGeneration:
    async def test_generation_count_and_bounds(self, db_session):
        org = await make_org(db_session, "Gen Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0), interval=60)
        created = await generate_slots_for_date(db_session, counter, MONDAY)
        assert len(created) == 8
        # Times stored in UTC: 09:00 IST → 03:30 UTC, 17:00 IST → 11:30 UTC
        assert created[0].start_time == local_to_utc_datetime(
            MONDAY, time(9, 0), "Asia/Kolkata"
        ).time()
        assert created[-1].end_time == local_to_utc_datetime(
            MONDAY, time(17, 0), "Asia/Kolkata"
        ).time()

    async def test_custom_interval_45_minutes(self, db_session):
        org = await make_org(db_session, "Gen 45 Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(12, 0), interval=45)
        created = await generate_slots_for_date(db_session, counter, MONDAY)
        assert len(created) == 4  # 9:00, 9:45, 10:30, 11:15

    async def test_idempotent_generation(self, db_session):
        org = await make_org(db_session, "Gen Idem Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(11, 0), interval=60)
        first = await generate_slots_for_date(db_session, counter, MONDAY)
        second = await generate_slots_for_date(db_session, counter, MONDAY)
        assert len(first) == 2
        assert second == []
        slots = await list_slots_for_date(db_session, counter.id, MONDAY)
        assert [s.id for s in slots] == [s.id for s in first]

    async def test_day_queue_counter_generates_nothing(self, db_session):
        org = await make_org(db_session, "Gen DQ Org")
        counter = await make_counter(db_session, org)  # day_queue
        created = await generate_slots_for_date(db_session, counter, MONDAY)
        assert created == []

    async def test_closed_day_generates_nothing(self, db_session):
        org = await make_org(db_session, "Gen Closed Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0), interval=60)
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.MAINTENANCE,
                      "Server maintenance")
        created = await generate_slots_for_date(db_session, counter, MONDAY)
        assert created == []

    async def test_slot_capacity_from_capacity_rule(self, db_session):
        org = await make_org(db_session, "Gen Cap Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(11, 0), interval=60)
        db_session.add(CapacityRule(
            organization_id=org.id, scope_level=ScheduleScopeLevel.COUNTER,
            scope_id=counter.id, limit_type=CapacityLimitType.SLOT_CAPACITY,
            booking_source=BookingSource.ALL, max_count=6,
        ))
        await db_session.flush()
        created = await generate_slots_for_date(db_session, counter, MONDAY)
        assert all(s.capacity == 6 for s in created)

    async def test_reduced_capacity_exception_scales_slots(self, db_session):
        org = await make_org(db_session, "Gen Reduced Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(11, 0), interval=60)
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.REDUCED_CAPACITY,
                      "Staff shortage", factor=Decimal("0.50"))
        created = await generate_slots_for_date(db_session, counter, MONDAY)
        assert all(s.capacity == 5 for s in created)  # 10 * 0.5

    async def test_default_slot_capacity(self, db_session):
        org = await make_org(db_session, "Gen Default Cap Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(10, 0), interval=60)
        created = await generate_slots_for_date(db_session, counter, MONDAY)
        assert all(s.capacity == svc.DEFAULT_SLOT_CAPACITY for s in created)

    async def test_ensure_slots_beyond_horizon_rejected(self, db_session):
        org = await make_org(db_session, "Gen Horizon Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(10, 0), interval=60)
        far = date.today() + timedelta(days=svc.MAX_ADVANCE_BOOKING_DAYS + 10)
        with pytest.raises(HTTPException) as exc:
            await ensure_slots_for_date(db_session, counter, far)
        assert exc.value.status_code == 400

    async def test_ensure_slots_past_date_generates_nothing(self, db_session):
        org = await make_org(db_session, "Gen Past Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(10, 0), interval=60)
        created = await ensure_slots_for_date(
            db_session, counter, date.today() - timedelta(days=1)
        )
        assert created == []


class TestSlotOccupancyStates:
    async def test_full_state(self, db_session):
        org = await make_org(db_session, "Occ Full Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(11, 0), interval=60)
        await generate_slots_for_date(db_session, counter, MONDAY)
        slots = await list_slots_for_date(db_session, counter.id, MONDAY)
        for s in slots:
            s.booked_count = s.capacity
        await db_session.flush()
        avail = await resolve_day_availability(db_session, org, MONDAY, counter=counter)
        assert avail.state == AvailabilityState.FULL

    async def test_limited_state(self, db_session):
        org = await make_org(db_session, "Occ Limited Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(11, 0), interval=60)
        await generate_slots_for_date(db_session, counter, MONDAY)
        slots = await list_slots_for_date(db_session, counter.id, MONDAY)
        slots[0].booked_count = slots[0].capacity      # full
        slots[1].booked_count = slots[1].capacity - 2  # 2 left of 10
        await db_session.flush()
        avail = await resolve_day_availability(db_session, org, MONDAY, counter=counter)
        assert avail.state == AvailabilityState.LIMITED

    async def test_available_state_with_free_slots(self, db_session):
        org = await make_org(db_session, "Occ Avail Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(11, 0), interval=60)
        await generate_slots_for_date(db_session, counter, MONDAY)
        avail = await resolve_day_availability(db_session, org, MONDAY, counter=counter)
        assert avail.state == AvailabilityState.AVAILABLE


class TestInactiveEntities:
    async def test_inactive_service_unavailable(self, db_session):
        org = await make_org(db_session, "Inact Svc Org")
        service = await make_service(db_session, org, "Inactive Svc")
        service.is_active = False
        avail = await resolve_day_availability(
            db_session, org, MONDAY, service=service
        )
        assert avail.state == AvailabilityState.SERVICE_UNAVAILABLE

    async def test_inactive_branch_not_open(self, db_session):
        org = await make_org(db_session, "Inact Branch Org")
        branch = await make_branch(db_session, org.id)
        branch.is_active = False
        avail = await resolve_day_availability(db_session, org, MONDAY, branch=branch)
        assert avail.state == AvailabilityState.NOT_OPEN

    async def test_inactive_counter_not_open(self, db_session):
        org = await make_org(db_session, "Inact Counter Org")
        counter = await make_counter(db_session, org)
        counter.status = "inactive"
        avail = await resolve_day_availability(db_session, org, MONDAY, counter=counter)
        assert avail.state == AvailabilityState.NOT_OPEN

    async def test_suspended_organization_not_open(self, db_session):
        org = await make_org(db_session, "Suspended Org")
        org.status = "suspended"
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.state == AvailabilityState.NOT_OPEN

    async def test_pending_organization_not_open(self, db_session):
        org = await make_org(db_session, "Pending Org")
        org.status = "pending"
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.state == AvailabilityState.NOT_OPEN


class TestPublishedCalendar:
    async def test_default_horizon_is_at_least_one_month(self, db_session):
        org = await make_org(db_session, "Cal Org")
        results = await get_published_availability(db_session, org, MONDAY)
        assert len(results) == svc.PUBLISH_HORIZON_DAYS
        assert svc.PUBLISH_HORIZON_DAYS >= 31
        assert all(isinstance(r.state, AvailabilityState) for r in results)
        assert all(r.timezone_name == "Asia/Kolkata" for r in results)

    async def test_slot_generation_across_calendar(self, db_session):
        org = await make_org(db_session, "Cal Slot Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(10, 0), interval=60)
        await get_published_availability(db_session, org, MONDAY, counter=counter)
        # Every Monday in the window has exactly one slot generated
        mondays = [
            MONDAY + timedelta(weeks=i)
            for i in range((svc.PUBLISH_HORIZON_DAYS + 6) // 7 + 1)
            if MONDAY + timedelta(weeks=i) < MONDAY + timedelta(days=svc.PUBLISH_HORIZON_DAYS)
        ]
        for d in mondays:
            assert len(await list_slots_for_date(db_session, counter.id, d)) == 1

    async def test_invalid_days_window_rejected(self, db_session):
        org = await make_org(db_session, "Cal Bad Org")
        with pytest.raises(HTTPException):
            await get_published_availability(db_session, org, MONDAY, days=0)
        with pytest.raises(HTTPException):
            await get_published_availability(
                db_session, org, MONDAY, days=svc.MAX_ADVANCE_BOOKING_DAYS + 1
            )

    async def test_invalid_timezone_rejected(self, db_session):
        org = await make_org(db_session, "Cal Bad TZ Org")
        branch = await make_branch(db_session, org.id)
        branch.timezone = "Mars/Olympus"
        with pytest.raises(HTTPException) as exc:
            await resolve_day_availability(db_session, org, MONDAY, branch=branch)
        assert exc.value.status_code == 400

    def test_utc_conversion_roundtrip(self):
        utc = local_to_utc_datetime(MONDAY, time(14, 15), "Asia/Kolkata")
        back = utc.astimezone(ZoneInfo("Asia/Kolkata"))
        assert (back.hour, back.minute) == (14, 15)


class TestReviewFixesD1D2D3:
    """Regressions for the three defects confirmed in the Task 2 review."""

    # ── D2: calendar path must not generate slots for past dates ──
    async def test_calendar_past_dates_generate_no_slots(self, db_session):
        org = await make_org(db_session, "Fix Past Cal Org")
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        for dow in range(7):
            await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                               org.id, dow, time(9, 0), time(10, 0), interval=60)
        start = date.today() - timedelta(days=3)
        results = await get_published_availability(
            db_session, org, start, days=7, counter=counter
        )
        assert len(results) == 7
        # "today" must be evaluated in the tenant timezone, not the host clock
        today_tenant = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        for offset in range(7):
            d = start + timedelta(days=offset)
            n = len(await list_slots_for_date(db_session, counter.id, d))
            if d < today_tenant:
                assert n == 0, f"slots generated for past date {d}"
            else:
                assert n == 1

    # ── D3: legacy path must honor active date exceptions ──
    async def test_legacy_reduced_capacity_exception_surfaces_factor(self, db_session):
        org = await make_org(db_session, "Fix Legacy RC Org")
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.REDUCED_CAPACITY,
                      "Staff shortage", factor=Decimal("0.50"))
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.state == AvailabilityState.AVAILABLE
        assert avail.is_legacy_day_queue is True
        assert avail.capacity_factor == Decimal("0.50")
        assert avail.exception is not None

    async def test_legacy_special_hours_exception_honored(self, db_session):
        org = await make_org(db_session, "Fix Legacy SH Org")
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.SPECIAL_HOURS,
                      "Short day", opens=time(11, 0), closes=time(13, 0))
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.state == AvailabilityState.AVAILABLE
        assert avail.is_legacy_day_queue is True
        assert (avail.opens_at, avail.closes_at) == (time(11, 0), time(13, 0))

    async def test_legacy_special_hours_without_window_not_open(self, db_session):
        org = await make_org(db_session, "Fix Legacy SH2 Org")
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.SPECIAL_HOURS,
                      "Unconfigured window")
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.state == AvailabilityState.NOT_OPEN
        assert avail.is_legacy_day_queue is True

    async def test_legacy_holiday_still_closes(self, db_session):
        org = await make_org(db_session, "Fix Legacy Hol Org")
        add_exception(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                      org.id, MONDAY, ScheduleExceptionType.HOLIDAY, "Diwali")
        avail = await resolve_day_availability(db_session, org, MONDAY)
        assert avail.state == AvailabilityState.HOLIDAY
        assert avail.reason == "Diwali"

    # ── D1: UTC date-wrap must fail loudly instead of storing wrong times ──
    async def test_utc_wrap_start_raises(self, db_session):
        # 05:00 IST → 23:30 UTC on the previous day: unrepresentable.
        org = await make_org(db_session, "Fix Wrap Org")
        org.timezone = "Asia/Kolkata"
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(5, 0), time(6, 0), interval=60)
        with pytest.raises(HTTPException) as exc:
            await generate_slots_for_date(db_session, counter, MONDAY, organization=org)
        assert exc.value.status_code == 400
        assert await list_slots_for_date(db_session, counter.id, MONDAY) == []

    async def test_utc_wrap_end_boundary_raises(self, db_session):
        # EST (UTC-5 in November): 18:30 local → 23:30 UTC is fine, but a
        # slot ending 19:30 local wraps to 00:30 UTC the next day.
        org = await make_org(db_session, "Fix Wrap End Org")
        org.timezone = "America/New_York"
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(17, 30), time(19, 30), interval=60)
        with pytest.raises(HTTPException) as exc:
            await generate_slots_for_date(db_session, counter, MONDAY, organization=org)
        assert exc.value.status_code == 400

    async def test_negative_offset_same_day_generates(self, db_session):
        # EST hours fully inside the same UTC date generate normally.
        org = await make_org(db_session, "Fix EST Ok Org")
        org.timezone = "America/New_York"
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await add_schedule(db_session, org.id, ScheduleScopeLevel.ORGANIZATION,
                           org.id, 0, time(9, 0), time(17, 0), interval=60)
        created = await generate_slots_for_date(db_session, counter, MONDAY, organization=org)
        assert len(created) == 8
        assert created[0].start_time == time(14, 0)  # 09:00 EST → 14:00 UTC
        assert created[-1].end_time == time(22, 0)   # 17:00 EST → 22:00 UTC
