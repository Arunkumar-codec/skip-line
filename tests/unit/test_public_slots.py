"""
tests/unit/test_public_slots.py
──────────────────────────────
SCH-009 slot-ladder rendering (Decision 12): the stored
(slot_date, UTC time-of-day) pairs are rendered back to the tenant
timezone (branch override wins), paired with the absolute UTC instants,
gated by the same-day cutoff (SCH-005 — started slots are never listed)
and the SCH-008 day states. Pure read/render view: no capacity semantics
beyond cap − booked − held, no token side effects.
"""

import datetime as dt
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import schedule_service
from app.services.schedule_service import (
    AvailabilityState,
    generate_slots_for_date,
    get_public_slots_for_date,
    list_slots_for_date,
)
from models.branch import Branch
from models.counter import Counter, QueueMode
from models.organization import Organization
from models.schedule_exception import ScheduleException, ScheduleExceptionType
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule
import models  # noqa: F401  — register all mappers

_org_seq = 0

KOLKATA = "Asia/Kolkata"
DUBAI = "Asia/Dubai"   # +04:00 — daytime hours stay on the same UTC date


async def make_org(db, timezone=KOLKATA) -> Organization:
    global _org_seq
    _org_seq += 1
    name = f"Slot Ladder Org {_org_seq}"
    org = Organization(
        name=name,
        registration_number=f"REG-{abs(hash(name)) % 10**8}",
        org_type="hospital",
        address="Test Address",
        city="Test City",
        state="Test State",
        email=f"slotladder{_org_seq}@test.example",
        mobile="9000000000",
        password_hash="x",
        status="approved",
        timezone=timezone,
    )
    db.add(org)
    await db.flush()
    return org


async def make_branch(db, org, name: str = "Main Branch") -> Branch:
    branch = Branch(
        organization_id=org.id,
        name=name,
        branch_code=f"SL-{abs(hash(name)) % 10**5}",
        address="Branch Address",
        city="Test City",
        state="Test State",
        pincode="000000",
    )
    db.add(branch)
    await db.flush()
    return branch


async def make_counter(db, org, mode=QueueMode.SLOTS, branch: Branch | None = None):
    counter = Counter(
        organization_id=org.id,
        branch_id=branch.id if branch is not None else None,
        counter_name=f"Ladder Counter {org.id}",
        counter_number=abs(hash((org.id, branch.id if branch else 0))) % 90 + 1,
        token_prefix=f"SL{org.id}",
        queue_mode=mode,
    )
    db.add(counter)
    await db.flush()
    return counter


async def publish_hours(db, org, counter=None, opens=time(10, 0),
                        closes=time(14, 0), interval=60):
    """Org-level (or counter-level) 10:00–14:00 business-local hours."""
    scope = (
        ScheduleScopeLevel.COUNTER if counter is not None
        else ScheduleScopeLevel.ORGANIZATION
    )
    scope_id = counter.id if counter is not None else org.id
    for dow in range(7):
        db.add(WeeklySchedule(
            organization_id=org.id,
            scope_level=scope,
            scope_id=scope_id,
            day_of_week=dow,
            open_time=opens,
            close_time=closes,
            slot_interval_minutes=interval,
        ))
    await db.flush()


def freeze_tenant_clock(monkeypatch, frozen_today: date):
    """Freeze the discovery clock seams consistently: tenant date AND a
    UTC instant whose time-of-day lies on the same UTC calendar date
    (09:00 IST = 03:30 UTC) — required because today's started-slot
    exclusion compares UTC time-of-day. 09:00 IST precedes every
    operating hour."""
    frozen_now = dt.datetime.combine(
        frozen_today, time(9, 0), tzinfo=ZoneInfo(KOLKATA)
    ).astimezone(dt.timezone.utc)
    monkeypatch.setattr(schedule_service, "get_tenant_today",
                        lambda _o, _b: frozen_today)
    monkeypatch.setattr(schedule_service, "_utcnow", lambda: frozen_now)
    return frozen_now


# ── Normal rendering ─────────────────────────────────────────────────────────

class TestSlotLadderRendering:
    async def test_renders_local_times_from_utc_pairs(self, db_session):
        org = await make_org(db_session)          # Asia/Kolkata (+05:30)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org)
        ladder_day = date.today() + timedelta(days=5)
        await generate_slots_for_date(db_session, counter, ladder_day)

        day = await get_public_slots_for_date(db_session, org, ladder_day)

        assert day.date == ladder_day
        assert day.state == AvailabilityState.AVAILABLE
        assert day.timezone_name == KOLKATA
        assert day.opens_at == time(10, 0) and day.closes_at == time(14, 0)
        assert day.is_legacy_day_queue is False
        assert [(s.start_local, s.end_local) for s in day.slots] == [
            (time(10, 0), time(11, 0)), (time(11, 0), time(12, 0)),
            (time(12, 0), time(13, 0)), (time(13, 0), time(14, 0)),
        ]
        first = day.slots[0]
        assert first.capacity == first.remaining == 10  # DEFAULT_SLOT_CAPACITY
        assert first.is_bookable is True
        # 10:00 IST = 04:30 UTC, same UTC date (representability rule)
        assert first.start_utc == dt.datetime.combine(
            ladder_day, time(4, 30), tzinfo=dt.timezone.utc
        )
        assert first.end_utc == dt.datetime.combine(
            ladder_day, time(5, 30), tzinfo=dt.timezone.utc
        )

    async def test_branch_timezone_override_wins(self, db_session):
        org = await make_org(db_session, timezone=KOLKATA)
        branch = await make_branch(db_session, org)
        branch.timezone = DUBAI                    # +04:00 override
        await db_session.flush()
        counter = await make_counter(db_session, org, branch=branch)
        await publish_hours(db_session, org, counter=counter)
        ladder_day = date.today() + timedelta(days=6)
        await generate_slots_for_date(db_session, counter, ladder_day)

        ladder = await get_public_slots_for_date(
            db_session, org, ladder_day, branch=branch
        )
        assert ladder.timezone_name == DUBAI
        assert [s.start_local for s in ladder.slots] == [
            time(10, 0), time(11, 0), time(12, 0), time(13, 0)
        ]

        # Same physical slots WITHOUT the branch context render in the org
        # timezone: identical UTC instants, +01:30 shifted local wall clock.
        org_day = await get_public_slots_for_date(db_session, org, ladder_day)
        assert org_day.timezone_name == KOLKATA
        assert [s.start_local for s in org_day.slots] == [
            time(11, 30), time(12, 30), time(13, 30), time(14, 30)
        ]
        assert [s.id for s in org_day.slots] == [s.id for s in ladder.slots]
        assert org_day.slots[0].start_utc == ladder.slots[0].start_utc

    async def test_counter_and_branch_filters(self, db_session):
        org = await make_org(db_session)
        c1 = await make_counter(db_session, org)
        c2 = await make_counter(db_session, org)
        branch = await make_branch(db_session, org)
        c3 = await make_counter(db_session, org, branch=branch)
        await publish_hours(db_session, org)
        ladder_day = date.today() + timedelta(days=7)
        for c in (c1, c2, c3):
            await generate_slots_for_date(db_session, c, ladder_day)

        ladder_c1 = await get_public_slots_for_date(
            db_session, org, ladder_day, counter_id=c1.id
        )
        assert {s.counter_id for s in ladder_c1.slots} == {c1.id}

        ladder_branch = await get_public_slots_for_date(
            db_session, org, ladder_day, branch=branch
        )
        assert {s.counter_id for s in ladder_branch.slots} == {c3.id}

    async def test_foreign_counter_id_is_404(self, db_session):
        org1 = await make_org(db_session)
        org2 = await make_org(db_session)
        foreign = await make_counter(db_session, org2)
        await publish_hours(db_session, org1)
        ladder_day = date.today() + timedelta(days=5)

        with pytest.raises(HTTPException) as exc_info:
            await get_public_slots_for_date(
                db_session, org1, ladder_day, counter_id=foreign.id
            )
        assert exc_info.value.status_code == 404   # tenant-target indistinguishable


# ── Same-day cutoff & bookability flags ──────────────────────────────────────

class TestSlotLadderCutoffAndBookability:
    async def test_today_excludes_started_slots(self, db_session, monkeypatch):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org)
        frozen_today = date.today()
        freeze_tenant_clock(monkeypatch, frozen_today)
        await generate_slots_for_date(db_session, counter, frozen_today)

        day = await get_public_slots_for_date(db_session, org, frozen_today)
        assert [s.start_local for s in day.slots] == [
            time(10, 0), time(11, 0), time(12, 0), time(13, 0)
        ]

        # Advance the frozen instant to 11:30 local: the 10:00 and 11:00
        # slots have started, exactly as the booking engine would reject —
        # they vanish from the ladder instead of rendering as bookable.
        later_utc = dt.datetime.combine(
            frozen_today, time(11, 30), tzinfo=ZoneInfo(KOLKATA)
        ).astimezone(dt.timezone.utc)
        monkeypatch.setattr(schedule_service, "_utcnow", lambda: later_utc)
        day2 = await get_public_slots_for_date(db_session, org, frozen_today)
        assert [s.start_local for s in day2.slots] == [time(12, 0), time(13, 0)]
        # The aggregate state reflects what the ladder can still offer.
        assert day2.state != AvailabilityState.FULL

    async def test_filled_slot_visible_but_not_bookable(self, db_session, monkeypatch):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org)
        frozen_today = date.today()
        freeze_tenant_clock(monkeypatch, frozen_today)
        await generate_slots_for_date(db_session, counter, frozen_today)
        slots = await list_slots_for_date(db_session, counter.id, frozen_today)
        slots[0].booked_count = slots[0].capacity            # fully booked
        slots[1].booked_count = slots[1].capacity - 1        # 1 held place
        slots[1].held_count = 1
        await db_session.flush()

        day = await get_public_slots_for_date(db_session, org, frozen_today)
        first, second, third = day.slots[0], day.slots[1], day.slots[2]
        assert first.remaining == 0 and first.is_bookable is False
        assert second.remaining == 0 and second.is_bookable is False
        assert third.remaining == third.capacity and third.is_bookable is True

    async def test_full_day_state_when_nothing_bookable(self, db_session, monkeypatch):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, opens=time(10, 0), closes=time(12, 0))
        frozen_today = date.today()
        freeze_tenant_clock(monkeypatch, frozen_today)
        await generate_slots_for_date(db_session, counter, frozen_today)
        for slot in await list_slots_for_date(db_session, counter.id, frozen_today):
            slot.booked_count = slot.capacity
        await db_session.flush()

        day = await get_public_slots_for_date(db_session, org, frozen_today)
        assert all(not s.is_bookable for s in day.slots)
        assert day.state == AvailabilityState.FULL


# ── Day-state gating & legacy tenants ────────────────────────────────────────

class TestSlotLadderDayStates:
    async def test_holiday_returns_no_slots(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org)
        ladder_day = date.today() + timedelta(days=5)
        db_session.add(ScheduleException(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            exception_date=ladder_day,
            exception_type=ScheduleExceptionType.HOLIDAY,
            reason="Diwali",
        ))
        await db_session.flush()
        # Generation honors the closure (creates nothing); the ladder must
        # surface the state, not silently list inventory.
        assert await generate_slots_for_date(db_session, counter, ladder_day) == []

        day = await get_public_slots_for_date(db_session, org, ladder_day)
        assert day.state == AvailabilityState.HOLIDAY
        assert day.reason == "Diwali"
        assert day.slots == []

    async def test_not_open_when_target_weekday_unpublished(self, db_session):
        org2 = await make_org(db_session)
        for dow in range(6):   # Sunday (6) missing
            db_session.add(WeeklySchedule(
                organization_id=org2.id,
                scope_level=ScheduleScopeLevel.ORGANIZATION,
                scope_id=org2.id,
                day_of_week=dow,
                open_time=time(9, 0), close_time=time(12, 0),
                slot_interval_minutes=60,
            ))
        await db_session.flush()
        sunday = date.today() + timedelta(days=1)
        while sunday.weekday() != 6:
            sunday += timedelta(days=1)
        assert sunday <= date.today() + timedelta(
            days=schedule_service.MAX_ADVANCE_BOOKING_DAYS
        )

        day = await get_public_slots_for_date(db_session, org2, sunday)
        assert day.state == AvailabilityState.NOT_OPEN
        assert day.slots == []

    async def test_legacy_day_queue_tenants_surface_flag(self, db_session):
        org = await make_org(db_session)   # no schedules published at all
        day = await get_public_slots_for_date(
            db_session, org, date.today() + timedelta(days=2)
        )
        assert day.state == AvailabilityState.AVAILABLE
        assert day.is_legacy_day_queue is True
        assert day.opens_at is None and day.closes_at is None
        assert day.slots == []


# ── Horizon guards ───────────────────────────────────────────────────────────

class TestSlotLadderGuards:
    async def test_past_date_rejected(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(HTTPException) as exc_info:
            await get_public_slots_for_date(
                db_session, org, date.today() - timedelta(days=1)
            )
        assert exc_info.value.status_code == 400

    async def test_beyond_horizon_rejected(self, db_session):
        org = await make_org(db_session)
        far = date.today() + timedelta(
            days=schedule_service.MAX_ADVANCE_BOOKING_DAYS + 1
        )
        with pytest.raises(HTTPException) as exc_info:
            await get_public_slots_for_date(db_session, org, far)
        assert exc_info.value.status_code == 400

    async def test_last_horizon_day_accepted(self, db_session):
        org = await make_org(db_session)
        near = date.today() + timedelta(
            days=schedule_service.MAX_ADVANCE_BOOKING_DAYS
        )
        day = await get_public_slots_for_date(db_session, org, near)
        assert day.date == near
