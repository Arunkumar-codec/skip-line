"""
tests/unit/test_schedule_admin.py
─────────────────────────────────
Phase 3 schedule-management APIs, service layer: weekly-schedule CRUD,
schedule-exception CRUD and capacity-rule CRUD with scope validation,
tenant isolation, validation failures, duplicate conflicts, database
constraint errors, audit/booking regressions, and the schedule-change
safeguard (generated slots and existing bookings are never modified).

CRUD functions commit, so created rows persist in the shared session-scoped
test database — every test uses its own freshly created organization.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import schedule_service, token_service
from app.services.schedule_service import AvailabilityState
from models.branch import Branch
from models.capacity_rule import BookingSource, CapacityLimitType, CapacityRule
from models.counter import Counter, CounterStatus, QueueMode
from models.organization import Organization
from models.schedule_exception import ScheduleException, ScheduleExceptionType
from models.service import Service
from models.time_slot import TimeSlot
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule
import models  # noqa: F401  — register all mappers

_seq = 0


async def make_org(db: AsyncSession) -> Organization:
    global _seq
    _seq += 1
    org = Organization(
        name=f"Sched Admin Org {_seq}",
        registration_number=f"REG-SA-{_seq:05d}-{abs(hash(f'sa{_seq}')) % 10**6}",
        org_type="hospital",
        address="1 Schedule Way",
        city="Sched City",
        state="Sched State",
        email=f"schedadmin{_seq}@test.example",
        mobile="9000000000",
        password_hash="x",
        status="approved",
    )
    db.add(org)
    await db.flush()
    return org


async def make_org_b(db: AsyncSession) -> Organization:
    """A second tenant (for isolation tests) — distinct from make_org."""
    global _seq
    _seq += 1
    org = Organization(
        name=f"Sched Admin Org B {_seq}",
        registration_number=f"REG-SB-{_seq:05d}-{abs(hash(f'sb{_seq}')) % 10**6}",
        org_type="bank",
        address="2 Other Way",
        city="Other City",
        state="Other State",
        email=f"schedadminb{_seq}@test.example",
        mobile="9000000001",
        password_hash="x",
        status="approved",
    )
    db.add(org)
    await db.flush()
    return org


async def make_branch(db: AsyncSession, org: Organization) -> Branch:
    branch = Branch(
        organization_id=org.id,
        name="Branch Main",
        branch_code=f"B{abs(hash(org.name)) % 10**4}",
        address="Branch Address",
        city=org.city,
        state=org.state,
        pincode="560001",
    )
    db.add(branch)
    await db.flush()
    return branch


async def make_counter(
    db: AsyncSession, org: Organization, active: bool = True
) -> Counter:
    counter = Counter(
        organization_id=org.id,
        counter_name=f"Counter {org.id}",
        counter_number=abs(hash(org.name)) % 100 + 1,
        token_prefix="W",
        queue_mode=QueueMode.SLOTS,
        status=CounterStatus.ACTIVE if active else CounterStatus.INACTIVE,
    )
    db.add(counter)
    await db.flush()
    return counter


async def make_service(
    db: AsyncSession, org: Organization, active: bool = True
) -> Service:
    service = Service(
        organization_id=org.id,
        name=f"Service {org.id}",
        is_active=active,
    )
    db.add(service)
    await db.flush()
    return service


# ── Weekly schedule CRUD ──────────────────────────────────────────────────────

class TestWeeklyScheduleCRUD:
    async def test_create_org_level_row(self, db_session):
        org = await make_org(db_session)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id,
            "day_of_week": 0,
            "open_time": time(9, 0), "close_time": time(17, 0),
            "slot_interval_minutes": 30,
        })
        assert row.id is not None
        assert row.scope_level == ScheduleScopeLevel.ORGANIZATION
        assert row.open_time == time(9, 0)
        assert row.slot_interval_minutes == 30

        listed = await schedule_service.list_weekly_schedules(db_session, org.id)
        assert [r.id for r in listed] == [row.id]

    async def test_create_closed_day_clears_configuration(self, db_session):
        org = await make_org(db_session)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 6,
            "is_closed": True,
            "open_time": time(9, 0), "close_time": time(17, 0),
            "slot_interval_minutes": 30,
        })
        assert row.is_closed is True
        assert row.open_time is None
        assert row.close_time is None
        assert row.slot_interval_minutes is None

    async def test_create_requires_hours_when_not_closed(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(ValueError, match="Operating hours are required"):
            await schedule_service.create_weekly_schedule(db_session, org.id, {
                "scope_level": "org", "scope_id": org.id, "day_of_week": 1,
            })

    async def test_create_rejects_reversed_hours(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(ValueError, match="earlier than close_time"):
            await schedule_service.create_weekly_schedule(db_session, org.id, {
                "scope_level": "org", "scope_id": org.id, "day_of_week": 1,
                "open_time": time(17, 0), "close_time": time(9, 0),
            })

    async def test_create_counter_scope_row(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "counter", "scope_id": counter.id, "day_of_week": 2,
            "open_time": time(10, 0), "close_time": time(14, 0),
        })
        assert row.scope_level == ScheduleScopeLevel.COUNTER
        assert row.slot_interval_minutes is None  # day-queue operating mode

    async def test_duplicate_scope_weekday_rejected(self, db_session):
        org = await make_org(db_session)
        payload = {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 3,
            "open_time": time(9, 0), "close_time": time(17, 0),
        }
        await schedule_service.create_weekly_schedule(db_session, org.id, dict(payload))
        with pytest.raises(ValueError, match="already exists"):
            await schedule_service.create_weekly_schedule(db_session, org.id, dict(payload))

    async def test_update_hours_and_day(self, db_session):
        org = await make_org(db_session)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 4,
            "open_time": time(9, 0), "close_time": time(17, 0),
        })
        updated = await schedule_service.update_weekly_schedule(db_session, org.id, row.id, {
            "day_of_week": 5, "open_time": time(10, 0),
            "slot_interval_minutes": 15,
        })
        assert updated.day_of_week == 5
        assert updated.open_time == time(10, 0)
        assert updated.slot_interval_minutes == 15

    async def test_update_to_closed_then_reopen(self, db_session):
        org = await make_org(db_session)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 1,
            "open_time": time(9, 0), "close_time": time(17, 0),
        })
        closed = await schedule_service.update_weekly_schedule(
            db_session, org.id, row.id, {"is_closed": True}
        )
        assert closed.is_closed is True and closed.open_time is None

        # Reopening without hours fails
        with pytest.raises(ValueError, match="Operating hours"):
            await schedule_service.update_weekly_schedule(
                db_session, org.id, row.id, {"is_closed": False}
            )
        # Reopening with hours works
        reopened = await schedule_service.update_weekly_schedule(
            db_session, org.id, row.id,
            {"is_closed": False, "open_time": time(8, 0), "close_time": time(12, 0)},
        )
        assert reopened.is_closed is False
        assert reopened.open_time == time(8, 0)
        assert reopened.close_time == time(12, 0)

    async def test_update_reopen_via_hours_only(self, db_session):
        org = await make_org(db_session)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 2,
            "is_closed": True,
        })
        reopened = await schedule_service.update_weekly_schedule(
            db_session, org.id, row.id,
            {"open_time": time(9, 0), "close_time": time(13, 0)},
        )
        assert reopened.is_closed is False

    async def test_update_scope_move_validated(self, db_session):
        org = await make_org(db_session)
        service = await make_service(db_session, org)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 0,
            "open_time": time(9, 0), "close_time": time(17, 0),
        })
        moved = await schedule_service.update_weekly_schedule(db_session, org.id, row.id, {
            "scope_level": "service", "scope_id": service.id,
        })
        assert moved.scope_level == ScheduleScopeLevel.SERVICE
        assert moved.scope_id == service.id

    async def test_update_scope_move_requires_both_fields(self, db_session):
        org = await make_org(db_session)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 0,
            "open_time": time(9, 0), "close_time": time(17, 0),
        })
        with pytest.raises(ValueError, match="together"):
            await schedule_service.update_weekly_schedule(
                db_session, org.id, row.id, {"scope_id": 123}
            )

    async def test_update_to_conflicting_weekday_rejected(self, db_session):
        org = await make_org(db_session)
        first = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 0,
            "open_time": time(9, 0), "close_time": time(17, 0),
        })
        second = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 1,
            "open_time": time(9, 0), "close_time": time(17, 0),
        })
        with pytest.raises(ValueError, match="already exists"):
            await schedule_service.update_weekly_schedule(
                db_session, org.id, second.id, {"day_of_week": 0}
            )
        # state unchanged
        fetched = await schedule_service.get_weekly_schedule(db_session, org.id, second.id)
        assert fetched.day_of_week == 1

    async def test_delete_row(self, db_session):
        org = await make_org(db_session)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 1,
            "open_time": time(9, 0), "close_time": time(17, 0),
        })
        await schedule_service.delete_weekly_schedule(db_session, org.id, row.id)
        with pytest.raises(ValueError, match="not found"):
            await schedule_service.get_weekly_schedule(db_session, org.id, row.id)


# ── Scope validation & tenant isolation ──────────────────────────────────────

class TestScopeValidationAndIsolation:
    async def test_org_scope_requires_own_id(self, db_session):
        org = await make_org(db_session)
        other = await make_org_b(db_session)
        with pytest.raises(ValueError, match="must use this organization"):
            await schedule_service.create_weekly_schedule(db_session, org.id, {
                "scope_level": "org", "scope_id": other.id, "day_of_week": 0,
                "open_time": time(9, 0), "close_time": time(17, 0),
            })

    async def test_cross_tenant_branch_rejected(self, db_session):
        org = await make_org(db_session)
        other = await make_org_b(db_session)
        branch = await make_branch(db_session, other)
        with pytest.raises(ValueError, match="Branch not found"):
            await schedule_service.create_weekly_schedule(db_session, org.id, {
                "scope_level": "branch", "scope_id": branch.id, "day_of_week": 0,
                "open_time": time(9, 0), "close_time": time(17, 0),
            })

    async def test_inactive_counter_rejected(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org, active=False)
        with pytest.raises(ValueError, match="Counter is not active"):
            await schedule_service.create_weekly_schedule(db_session, org.id, {
                "scope_level": "counter", "scope_id": counter.id, "day_of_week": 0,
                "open_time": time(9, 0), "close_time": time(17, 0),
            })

    async def test_inactive_service_rejected(self, db_session):
        org = await make_org(db_session)
        service = await make_service(db_session, org, active=False)
        with pytest.raises(ValueError, match="Service is not active"):
            await schedule_service.create_weekly_schedule(db_session, org.id, {
                "scope_level": "service", "scope_id": service.id, "day_of_week": 0,
                "open_time": time(9, 0), "close_time": time(17, 0),
            })

    async def test_cross_tenant_read_update_delete_blocked(self, db_session):
        org = await make_org(db_session)
        other = await make_org_b(db_session)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 0,
            "open_time": time(9, 0), "close_time": time(17, 0),
        })
        with pytest.raises(ValueError, match="not found"):
            await schedule_service.get_weekly_schedule(db_session, other.id, row.id)
        with pytest.raises(ValueError, match="not found"):
            await schedule_service.update_weekly_schedule(
                db_session, other.id, row.id, {"open_time": time(8, 0)}
            )
        with pytest.raises(ValueError, match="not found"):
            await schedule_service.delete_weekly_schedule(db_session, other.id, row.id)
        assert await schedule_service.list_weekly_schedules(db_session, other.id) == []

    async def test_list_scoped_to_own_organization(self, db_session):
        org = await make_org(db_session)
        other = await make_org_b(db_session)
        await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id, "day_of_week": 0,
            "open_time": time(9, 0), "close_time": time(17, 0),
        })
        mine = await schedule_service.list_weekly_schedules(db_session, org.id)
        assert all(r.organization_id == org.id for r in mine)
        assert await schedule_service.list_weekly_schedules(db_session, other.id) == []


# ── Schedule exception CRUD ──────────────────────────────────────────────────

class TestScheduleExceptionCRUD:
    def _payload(self, org, **over):
        p = {
            "scope_level": "org", "scope_id": org.id,
            "exception_date": date.today() + timedelta(days=10),
            "exception_type": "holiday",
            "reason": "Annual day closure",
        }
        p.update(over)
        return p

    async def test_create_holiday(self, db_session):
        org = await make_org(db_session)
        row = await schedule_service.create_schedule_exception(
            db_session, org.id, self._payload(org)
        )
        assert row.exception_type == ScheduleExceptionType.HOLIDAY
        assert row.capacity_factor is None

    async def test_duplicate_scope_date_rejected(self, db_session):
        org = await make_org(db_session)
        await schedule_service.create_schedule_exception(db_session, org.id, self._payload(org))
        with pytest.raises(ValueError, match="already exists"):
            await schedule_service.create_schedule_exception(
                db_session, org.id, self._payload(org, reason="Another reason")
            )

    async def test_special_hours_window_validated(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(ValueError, match="require opens_at and closes_at"):
            await schedule_service.create_schedule_exception(db_session, org.id, self._payload(
                org, exception_type="special_hours"
            ))
        with pytest.raises(ValueError, match="open before it closes"):
            await schedule_service.create_schedule_exception(db_session, org.id, self._payload(
                org, exception_type="special_hours",
                opens_at=time(17, 0), closes_at=time(9, 0),
            ))
        with pytest.raises(ValueError, match="only to reduced_capacity"):
            await schedule_service.create_schedule_exception(db_session, org.id, self._payload(
                org, exception_type="special_hours",
                opens_at=time(9, 0), closes_at=time(13, 0),
                capacity_factor=Decimal("0.50"),
            ))

    async def test_reduced_capacity_requires_factor(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(ValueError, match="require a capacity_factor"):
            await schedule_service.create_schedule_exception(db_session, org.id, self._payload(
                org, exception_type="reduced_capacity"
            ))
        with pytest.raises(ValueError, match="special_hours, not reduced_capacity"):
            await schedule_service.create_schedule_exception(db_session, org.id, self._payload(
                org, exception_type="reduced_capacity",
                capacity_factor=Decimal("0.50"),
                opens_at=time(9, 0), closes_at=time(17, 0),
            ))

    async def test_closure_type_rejects_extra_fields(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(ValueError, match="carry only a reason"):
            await schedule_service.create_schedule_exception(db_session, org.id, self._payload(
                org, capacity_factor=Decimal("0.50")
            ))
        with pytest.raises(ValueError, match="carry only a reason"):
            await schedule_service.create_schedule_exception(db_session, org.id, self._payload(
                org, opens_at=time(9, 0), closes_at=time(17, 0)
            ))

    async def test_update_and_deactivate(self, db_session):
        org = await make_org(db_session)
        row = await schedule_service.create_schedule_exception(
            db_session, org.id, self._payload(org)
        )
        updated = await schedule_service.update_schedule_exception(
            db_session, org.id, row.id, {"is_active": False, "reason": "Covid protocol"}
        )
        assert updated.is_active is False
        assert updated.reason == "Covid protocol"

    async def test_update_to_conflicting_date_rejected(self, db_session):
        org = await make_org(db_session)
        day1 = date.today() + timedelta(days=11)
        day2 = date.today() + timedelta(days=12)
        first = await schedule_service.create_schedule_exception(
            db_session, org.id, self._payload(org, exception_date=day1)
        )
        second = await schedule_service.create_schedule_exception(
            db_session, org.id, self._payload(org, exception_date=day2)
        )
        with pytest.raises(ValueError, match="already exists"):
            await schedule_service.update_schedule_exception(
                db_session, org.id, second.id, {"exception_date": day1}
            )
        fetched = await schedule_service.get_schedule_exception(db_session, org.id, second.id)
        assert fetched.exception_date == day2
        assert first.id != second.id

    async def test_list_date_filters(self, db_session):
        org = await make_org(db_session)
        near = date.today() + timedelta(days=5)
        far = date.today() + timedelta(days=40)
        await schedule_service.create_schedule_exception(
            db_session, org.id, self._payload(org, exception_date=near)
        )
        await schedule_service.create_schedule_exception(
            db_session, org.id, self._payload(org, exception_date=far, reason="Far holiday")
        )
        rows = await schedule_service.list_schedule_exceptions(
            db_session, org.id, date_from=date.today() + timedelta(days=1),
            date_to=date.today() + timedelta(days=20),
        )
        assert [r.exception_date for r in rows] == [near]

    async def test_delete(self, db_session):
        org = await make_org(db_session)
        row = await schedule_service.create_schedule_exception(
            db_session, org.id, self._payload(org)
        )
        await schedule_service.delete_schedule_exception(db_session, org.id, row.id)
        with pytest.raises(ValueError, match="not found"):
            await schedule_service.get_schedule_exception(db_session, org.id, row.id)


# ── Capacity rule CRUD ───────────────────────────────────────────────────────

class TestCapacityRuleCRUD:
    async def test_create_counter_slot_capacity_rule(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        row = await schedule_service.create_capacity_rule(db_session, org.id, {
            "scope_level": "counter", "scope_id": counter.id,
            "limit_type": "slot_capacity", "booking_source": "all",
            "max_count": 6,
        })
        assert row.limit_type == CapacityLimitType.SLOT_CAPACITY
        assert row.max_count == 6
        # The booking engine consumes it (rule-driven capacity)
        capacity = await schedule_service.get_slot_capacity(
            db_session, org.id, counter
        )
        assert capacity == 6

    async def test_duplicate_type_source_rejected(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        payload = {
            "scope_level": "counter", "scope_id": counter.id,
            "limit_type": "slot_capacity", "booking_source": "all",
            "max_count": 5,
        }
        await schedule_service.create_capacity_rule(db_session, org.id, dict(payload))
        with pytest.raises(ValueError, match="already exists"):
            await schedule_service.create_capacity_rule(db_session, org.id, dict(payload))
        # same limit type, different source is a distinct rule
        other = await schedule_service.create_capacity_rule(db_session, org.id, {
            **payload, "booking_source": "online", "max_count": 3,
        })
        assert other.booking_source == BookingSource.ONLINE

    async def test_negative_max_count_rejected(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(ValueError, match="0 or greater"):
            await schedule_service.create_capacity_rule(db_session, org.id, {
                "scope_level": "org", "scope_id": org.id,
                "limit_type": "daily_token", "max_count": -1,
            })

    async def test_update_and_delete(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        row = await schedule_service.create_capacity_rule(db_session, org.id, {
            "scope_level": "counter", "scope_id": counter.id,
            "limit_type": "slot_capacity", "max_count": 5,
        })
        updated = await schedule_service.update_capacity_rule(
            db_session, org.id, row.id, {"max_count": 9, "is_active": False}
        )
        assert updated.max_count == 9
        assert updated.is_active is False

        await schedule_service.delete_capacity_rule(db_session, org.id, row.id)
        with pytest.raises(ValueError, match="not found"):
            await schedule_service.get_capacity_rule(db_session, org.id, row.id)

    async def test_cross_tenant_rule_blocked(self, db_session):
        org = await make_org(db_session)
        other = await make_org_b(db_session)
        counter = await make_counter(db_session, org)
        row = await schedule_service.create_capacity_rule(db_session, org.id, {
            "scope_level": "counter", "scope_id": counter.id,
            "limit_type": "slot_capacity", "max_count": 5,
        })
        with pytest.raises(ValueError, match="not found"):
            await schedule_service.get_capacity_rule(db_session, other.id, row.id)
        with pytest.raises(ValueError, match="not found"):
            await schedule_service.update_capacity_rule(
                db_session, other.id, row.id, {"max_count": 1}
            )
        with pytest.raises(ValueError, match="not found"):
            await schedule_service.delete_capacity_rule(db_session, other.id, row.id)


# ── Schedule-change safeguards & booking regressions ─────────────────────────

class TestScheduleChangeSafeguards:
    async def test_editing_hours_leaves_generated_slots_untouched(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id,
            "day_of_week": date.today().weekday(),
            "open_time": time(9, 0), "close_time": time(17, 0),
            "slot_interval_minutes": 60,
        })
        await schedule_service.generate_slots_for_date(db_session, counter, date.today())
        before = await schedule_service.list_slots_for_date(db_session, counter.id, date.today())
        assert len(before) == 8

        # Change the interval via CRUD — generated slots must not be rewritten
        await schedule_service.update_weekly_schedule(
            db_session, org.id, row.id, {"slot_interval_minutes": 30}
        )
        after = await schedule_service.list_slots_for_date(db_session, counter.id, date.today())
        assert [(s.start_time, s.capacity, s.booked_count) for s in after] == [
            (s.start_time, s.capacity, s.booked_count) for s in before
        ]

        # Deleting the row must not delete generated slots either
        await schedule_service.delete_weekly_schedule(db_session, org.id, row.id)
        remaining = await schedule_service.list_slots_for_date(db_session, counter.id, date.today())
        assert len(remaining) == 8

    async def test_existing_booking_preserved_when_hours_change(self, db_session, monkeypatch):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        # Freeze the SCH-005 cutoff clock to a pre-open instant of the real
        # tenant today so the day's first slot (09:00 IST) stays bookable
        # regardless of the real wall clock.
        frozen_now = datetime.combine(date.today(), time(3, 0), tzinfo=timezone.utc)
        monkeypatch.setattr(token_service, "_utcnow", lambda: frozen_now)
        row = await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id,
            "day_of_week": date.today().weekday(),
            "open_time": time(9, 0), "close_time": time(17, 0),
            "slot_interval_minutes": 60,
        })
        await schedule_service.generate_slots_for_date(db_session, counter, date.today())
        slot = (await schedule_service.list_slots_for_date(db_session, counter.id, date.today()))[0]
        token = await token_service.book_token(
            db=db_session, org_id=org.id, counter_id=counter.id,
            service_id=None, user_id=None, slot_id=slot.id,
        )
        await db_session.refresh(slot)
        assert slot.booked_count == 1

        # Schedule change after the booking — slot occupancy preserved
        await schedule_service.update_weekly_schedule(
            db_session, org.id, row.id, {"slot_interval_minutes": 30}
        )
        await db_session.refresh(slot)
        assert slot.booked_count == 1
        assert token.slot_id == slot.id

    async def test_crud_created_holiday_blocks_booking(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        target = date.today() + timedelta(days=7)
        await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id,
            "day_of_week": date.today().weekday(),
            "open_time": time(9, 0), "close_time": time(17, 0),
            "slot_interval_minutes": 60,
        })
        await schedule_service.generate_slots_for_date(db_session, counter, target)
        slot = (await schedule_service.list_slots_for_date(db_session, counter.id, target))[0]
        await schedule_service.create_schedule_exception(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id,
            "exception_date": target,
            "exception_type": "holiday",
            "reason": "Admin-configured closure",
        })

        with pytest.raises(ValueError, match="Admin-configured closure"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )
        await db_session.refresh(slot)
        assert slot.booked_count == 0

    async def test_public_slot_dates_reflect_crud_changes(
        self, db_session, monkeypatch
    ):
        # Freeze the SCH-005 cutoff before the first slot starts
        # (00:30 UTC = 06:00 IST): after 16:00 IST every one of today's
        # 09:00-17:00 IST slots has started and today legitimately renders
        # FULL, which would empty the initial bookable list by wall clock.
        frozen_today = date.today()
        monkeypatch.setattr(
            schedule_service, "get_tenant_today", lambda _o, _b: frozen_today
        )
        monkeypatch.setattr(
            schedule_service, "_utcnow",
            lambda: datetime.combine(frozen_today, time(0, 30), tzinfo=timezone.utc),
        )
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "org", "scope_id": org.id,
            "day_of_week": date.today().weekday(),
            "open_time": time(9, 0), "close_time": time(17, 0),
            "slot_interval_minutes": 60,
        })
        summaries = await schedule_service.get_public_slot_dates(db_session, org, days=3)
        bookable = [s for s in summaries if s.state == AvailabilityState.AVAILABLE]
        assert len(bookable) >= 1

        # Close everything via CRUD → no bookable dates remain
        rows = await schedule_service.list_weekly_schedules(db_session, org.id)
        for r in rows:
            await schedule_service.update_weekly_schedule(
                db_session, org.id, r.id, {"is_closed": True}
            )
        summaries = await schedule_service.get_public_slot_dates(db_session, org, days=3)
        blocking = {
            AvailabilityState.CLOSED,
            AvailabilityState.HOLIDAY,
            AvailabilityState.NOT_OPEN,
            AvailabilityState.SERVICE_UNAVAILABLE,
            AvailabilityState.FULL,
        }
        assert all(s.state in blocking for s in summaries)
        # the configured weekday is explicitly CLOSED now
        assert summaries[0].state == AvailabilityState.CLOSED
