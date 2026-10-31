"""
tests/unit/test_discovery.py
────────────────────────────
Phase 3 Task 4: enhanced organization discovery (Decision 15 scope —
service filter, branch/pincode filter, available slot-date filter, and
distance search via branch geo coordinates) plus the public slot-date
discovery endpoint and the inactive-service booking eligibility guard.

Search queries never commit; slot-date tests may commit via generate... no
book_token is used here, so flushed-only data suffices except where noted
(no book_token calls in this module — nothing persists across tests).
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import schedule_service, token_service, user_service
from app.services.schedule_service import (
    AvailabilityState,
    generate_slots_for_date,
    get_tenant_today,
    list_slots_for_date,
)
from app.services.user_service import _haversine_km
from models.branch import Branch
from models.counter import Counter, CounterStatus, QueueMode
from models.organization import Organization
from models.schedule_exception import ScheduleException, ScheduleExceptionType
from models.service import Service
from models.time_slot import TimeSlot
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule
import models  # noqa: F401  — register all mappers

_org_seq = 0


async def make_org(db: AsyncSession, name: str) -> Organization:
    global _org_seq
    _org_seq += 1
    org = Organization(
        name=name,
        registration_number=f"REG-DISC-{_org_seq:05d}-{abs(hash(name)) % 10**6}",
        org_type="hospital",
        address="1 Discovery Way",
        city="Bengaluru",
        state="Karnataka",
        email=f"disc{_org_seq}@test.example",
        mobile="9000000000",
        password_hash="x",
        status="approved",
    )
    db.add(org)
    await db.flush()
    return org


async def make_branch(
    db: AsyncSession,
    org: Organization,
    pincode: str = "560001",
    geo: tuple[float, float] | None = None,
    is_active: bool = True,
    name: str = "Main Branch",
) -> Branch:
    branch = Branch(
        organization_id=org.id,
        name=name,
        branch_code=f"B{abs(hash(name)) % 10**4}",
        address="Branch Address",
        city=org.city,
        state=org.state,
        pincode=pincode,
        is_active=is_active,
        geo_lat=geo[0] if geo else None,
        geo_lng=geo[1] if geo else None,
    )
    db.add(branch)
    await db.flush()
    return branch


async def make_service(
    db: AsyncSession, org: Organization, name: str, is_active: bool = True
) -> Service:
    service = Service(
        organization_id=org.id,
        name=name,
        service_code=name[:20],
        is_active=is_active,
    )
    db.add(service)
    await db.flush()
    return service


async def make_counter(
    db: AsyncSession,
    org: Organization,
    mode: QueueMode = QueueMode.SLOTS,
    service: Service | None = None,
    status: CounterStatus = CounterStatus.ACTIVE,
) -> Counter:
    counter = Counter(
        organization_id=org.id,
        service_id=service.id if service else None,
        counter_name=f"Counter {org.id}",
        counter_number=abs(hash(org.name)) % 100 + 1,
        token_prefix="D",
        queue_mode=mode,
        status=status,
    )
    db.add(counter)
    await db.flush()
    return counter


async def publish_hours(db: AsyncSession, org: Organization) -> None:
    """Org-level 9-17 hours every weekday with a 60-minute slot interval."""
    for dow in range(7):
        db.add(WeeklySchedule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            day_of_week=dow,
            open_time=time(9, 0),
            close_time=time(17, 0),
            slot_interval_minutes=60,
        ))
    await db.flush()


def future_slot_date(days_ahead: int = 3, org: Organization | None = None) -> date:
    """A bookable future date in the tenant timezone (Asia/Kolkata default)."""
    if org is not None:
        return get_tenant_today(org, None) + timedelta(days=days_ahead)
    return date.today() + timedelta(days=days_ahead)


# ── Service filter (SRCH-001/002) ────────────────────────────────────────────

class TestServiceFilter:
    async def test_matches_org_with_active_service(self, db_session):
        target = await make_org(db_session, "Disc Svc Target")
        other = await make_org(db_session, "Disc Svc Other")
        svc = await make_service(db_session, target, "X-Ray Booking")
        await make_service(db_session, other, "Other Service")

        orgs, total = await user_service.search_organizations(
            db_session, service_id=svc.id
        )
        assert total == 1
        assert [o.id for o in orgs] == [target.id]

    async def test_excludes_inactive_service(self, db_session):
        org = await make_org(db_session, "Disc Svc Inactive")
        svc = await make_service(db_session, org, "Retired Service", is_active=False)

        orgs, total = await user_service.search_organizations(
            db_session, service_id=svc.id
        )
        assert total == 0
        assert orgs == []

    async def test_unknown_service_id(self, db_session):
        orgs, total = await user_service.search_organizations(
            db_session, service_id=999999
        )
        assert total == 0
        assert orgs == []


# ── Pincode filter ───────────────────────────────────────────────────────────

class TestPincodeFilter:
    async def test_matches_active_branch_pincode(self, db_session):
        target = await make_org(db_session, "Disc Pin Target")
        other = await make_org(db_session, "Disc Pin Other")
        await make_branch(db_session, target, pincode="560102")
        await make_branch(db_session, other, pincode="560099")

        orgs, total = await user_service.search_organizations(
            db_session, pincode="560102"
        )
        assert total == 1
        assert [o.id for o in orgs] == [target.id]

    async def test_ignores_inactive_branch(self, db_session):
        org = await make_org(db_session, "Disc Pin Inactive")
        await make_branch(db_session, org, pincode="560200", is_active=False)

        orgs, total = await user_service.search_organizations(
            db_session, pincode="560200"
        )
        assert total == 0
        assert orgs == []


# ── Distance search ──────────────────────────────────────────────────────────

class TestDistanceSearch:
    async def test_haversine_known_pair(self):
        # 0.1 degrees of latitude ≈ 11.12 km on the same meridian
        d = _haversine_km(12.0, 77.0, 12.1, 77.0)
        assert 11.0 < d < 11.3

    async def test_nearest_first_sort(self, db_session):
        near = await make_org(db_session, "DisT1 Near Org")
        far = await make_org(db_session, "DisT1 Far Org")
        await make_branch(db_session, near, geo=(12.9716, 77.5946))
        await make_branch(db_session, far, geo=(13.0500, 77.6000))

        # Marker query scopes the assertion to this test's committed rows —
        # other test files leave committed approved orgs in the shared DB.
        orgs, total = await user_service.search_organizations(
            db_session, query="DisT1", lat=12.9716, lng=77.5946
        )
        assert total == 2
        assert [o.name for o in orgs] == [near.name, far.name]
        assert orgs[0].distance_km == pytest.approx(0.0, abs=0.001)
        assert orgs[1].distance_km > orgs[0].distance_km

    async def test_radius_excludes_far_and_ungeocoded(self, db_session):
        near = await make_org(db_session, "Disc Radius Near")
        far = await make_org(db_session, "Disc Radius Far")
        ungeocoded = await make_org(db_session, "Disc Radius NoGeo")
        await make_branch(db_session, near, geo=(12.9716, 77.5946))
        await make_branch(db_session, far, geo=(13.3000, 77.7000))
        await make_branch(db_session, ungeocoded)  # no geo

        orgs, total = await user_service.search_organizations(
            db_session, lat=12.9716, lng=77.5946, radius_km=5.0
        )
        assert total == 1
        assert [o.id for o in orgs] == [near.id]

    async def test_no_radius_keeps_ungeocoded_with_none_distance(self, db_session):
        geocoded = await make_org(db_session, "DisT2 Geo")
        plain = await make_org(db_session, "DisT2 Plain")
        await make_branch(db_session, geocoded, geo=(12.9716, 77.5946))
        await make_branch(db_session, plain)

        orgs, total = await user_service.search_organizations(
            db_session, query="DisT2", lat=12.9716, lng=77.5946
        )
        assert total == 2
        by_id = {o.id: o for o in orgs}
        assert by_id[geocoded.id].distance_km is not None
        assert by_id[plain.id].distance_km is None
        # ungeocoded sorts last
        assert orgs[-1].id == plain.id

    async def test_uses_nearest_geocoded_branch(self, db_session):
        org = await make_org(db_session, "Disc TwoBranches")
        await make_branch(db_session, org, geo=(13.2000, 77.7000), name="Far Branch")
        await make_branch(db_session, org, geo=(12.9720, 77.5950), name="Near Branch")

        orgs, _ = await user_service.search_organizations(
            db_session, lat=12.9716, lng=77.5946
        )
        assert orgs[0].id == org.id
        assert orgs[0].distance_km < 1.0

    async def test_no_coordinates_preserves_legacy_behavior(self, db_session):
        a = await make_org(db_session, "Disc Legacy A")
        b = await make_org(db_session, "Disc Legacy B")

        orgs, total = await user_service.search_organizations(
            db_session, query="Disc Legacy"
        )
        assert total == 2
        assert [o.name for o in orgs] == sorted([a.name, b.name])
        assert all(o.distance_km is None for o in orgs)

    async def test_invalid_radius_rejected(self, db_session):
        with pytest.raises(ValueError):
            await user_service.search_organizations(
                db_session, lat=12.0, lng=77.0, radius_km=0
            )


# ── Available slot-date filter (search) ──────────────────────────────────────

class TestSlotDateSearchFilter:
    async def test_matches_org_with_remaining_capacity(self, db_session):
        org = await make_org(db_session, "Disc SlotDate Hit")
        counter = await make_counter(db_session, org, QueueMode.SLOTS)
        await publish_hours(db_session, org)
        target = future_slot_date(days_ahead=25)
        await generate_slots_for_date(db_session, counter, target)

        orgs, total = await user_service.search_organizations(
            db_session, slot_date=target
        )
        assert total == 1
        assert [o.id for o in orgs] == [org.id]

    async def test_excludes_fully_booked_date(self, db_session):
        org = await make_org(db_session, "Disc SlotDate Full")
        counter = await make_counter(db_session, org, QueueMode.SLOTS)
        await publish_hours(db_session, org)
        target = future_slot_date(days_ahead=25)
        await generate_slots_for_date(db_session, counter, target)
        for slot in await list_slots_for_date(db_session, counter.id, target):
            slot.booked_count = slot.capacity

        orgs, total = await user_service.search_organizations(
            db_session, slot_date=target
        )
        assert total == 0
        assert orgs == []

    async def test_day_queue_only_org_not_matched(self, db_session):
        org = await make_org(db_session, "Disc SlotDate DayQ")
        await make_counter(db_session, org, QueueMode.DAY_QUEUE)

        orgs, total = await user_service.search_organizations(
            db_session, slot_date=future_slot_date(days_ahead=25)
        )
        assert total == 0
        assert orgs == []


# ── Public slot-date discovery ───────────────────────────────────────────────

class TestPublicSlotDates:
    async def test_legacy_tenant_dates_are_available_without_inventory(self, db_session):
        org = await make_org(db_session, "Disc Pub Legacy")

        summaries = await schedule_service.get_public_slot_dates(db_session, org, days=5)
        assert len(summaries) == 5
        assert all(s.state == AvailabilityState.AVAILABLE for s in summaries)
        assert all(s.remaining is None for s in summaries)
        assert all(s.is_legacy_day_queue for s in summaries)

    async def test_slot_dates_report_remaining_capacity(self, db_session, monkeypatch):
        org = await make_org(db_session, "Disc Pub Slots")
        counter = await make_counter(db_session, org, QueueMode.SLOTS)
        await publish_hours(db_session, org)

        # Freeze the SCH-005 cutoff clocks: no slot of the frozen day has
        # started at 03:00 UTC (08:30 IST), so today's remaining is the
        # full 80 regardless of the real wall clock.
        frozen_today = get_tenant_today(org, None)
        frozen_now = datetime.combine(frozen_today, time(3, 0), tzinfo=timezone.utc)
        monkeypatch.setattr(
            schedule_service, "get_tenant_today", lambda _o, _b: frozen_today
        )
        monkeypatch.setattr(schedule_service, "_utcnow", lambda: frozen_now)

        summaries = await schedule_service.get_public_slot_dates(db_session, org, days=3)
        assert len(summaries) == 3
        today = get_tenant_today(org, None)
        assert [s.date for s in summaries] == [
            today + timedelta(days=i) for i in range(3)
        ]
        for s in summaries:
            assert s.state == AvailabilityState.AVAILABLE
            # 9-17 with a 60-minute interval → 8 slots × 10 default capacity
            assert s.remaining == 80
            assert s.opens_at == time(9, 0)
            assert s.closes_at == time(17, 0)
        # slots were actually generated for the horizon
        from sqlalchemy import select
        slots = (
            await db_session.execute(
                select(TimeSlot).where(
                    TimeSlot.counter_id == counter.id,
                    TimeSlot.slot_date == today + timedelta(days=2),
                )
            )
        ).scalars().all()
        assert len(slots) == 8

    async def test_today_summary_excludes_started_slots(self, db_session, monkeypatch):
        """SCH-005 same-day cutoff, read side: today's advertised remaining
        capacity counts only slots that have not started yet (a started slot
        can never be booked — token_service rejects it), so its places are
        excluded from the day summary entirely."""
        from sqlalchemy import select

        org = await make_org(db_session, "Disc Pub Cutoff")
        counter = await make_counter(db_session, org, QueueMode.SLOTS)
        await publish_hours(db_session, org)

        frozen_today = date(2026, 11, 2)                     # Monday
        frozen_now = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
        monkeypatch.setattr(
            schedule_service, "get_tenant_today", lambda _o, _b: frozen_today
        )
        monkeypatch.setattr(schedule_service, "_utcnow", lambda: frozen_now)

        await generate_slots_for_date(db_session, counter, frozen_today)
        elapsed = (
            await db_session.execute(
                select(TimeSlot).where(
                    TimeSlot.counter_id == counter.id,
                    TimeSlot.slot_date == frozen_today,
                    TimeSlot.start_time == time(3, 30),   # 09:00 IST slot
                )
            )
        ).scalar_one()
        elapsed.booked_count = 3
        await db_session.flush()

        summaries = await schedule_service.get_public_slot_dates(db_session, org, days=2)
        today_summary, future_summary = summaries[0], summaries[1]
        assert today_summary.date == frozen_today
        # bookable inventory = 7 remaining slots × 10 = 70 (the elapsed slot's
        # 10 places AND its 3 bookings are excluded; naive math would say 77)
        assert today_summary.remaining == 70
        assert today_summary.state == AvailabilityState.AVAILABLE
        # a future date is unaffected by the cutoff
        assert future_summary.remaining == 80

    async def test_full_date_reports_full_state(self, db_session):
        org = await make_org(db_session, "Disc Pub Full")
        counter = await make_counter(db_session, org, QueueMode.SLOTS)
        await publish_hours(db_session, org)
        target = future_slot_date(days_ahead=3)
        await schedule_service.get_public_slot_dates(db_session, org, days=1)
        # fill every slot of today (the only generated date)
        today = get_tenant_today(org, None)
        for slot in await list_slots_for_date(db_session, counter.id, today):
            slot.booked_count = slot.capacity
        await db_session.flush()

        summaries = await schedule_service.get_public_slot_dates(db_session, org, days=1)
        assert summaries[0].state == AvailabilityState.FULL
        assert summaries[0].remaining == 0

    async def test_closed_weekday_surfaces_not_open(self, db_session, monkeypatch):
        # Freeze the same-day cutoff BEFORE any operating hour starts
        # (00:30 UTC = 06:00 IST): without a freeze this test depends on
        # the wall clock — after 16:00 IST every one of today's
        # 09:00-17:00 IST slots has started and today legitimately shifts
        # to FULL (SCH-005), flipping the single AVAILABLE summary off.
        frozen_today = date.today()
        monkeypatch.setattr(
            schedule_service, "get_tenant_today", lambda _o, _b: frozen_today
        )
        frozen_now = datetime.combine(
            frozen_today, time(0, 30), tzinfo=timezone.utc
        )
        monkeypatch.setattr(schedule_service, "_utcnow", lambda: frozen_now)

        org = await make_org(db_session, "Disc Pub Partial")
        counter = await make_counter(db_session, org, QueueMode.SLOTS)
        # publish hours for ONE weekday only; all other weekdays are NOT_OPEN
        db_session.add(WeeklySchedule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            day_of_week=schedule_service.get_tenant_today(org, None).weekday(),
            open_time=time(9, 0),
            close_time=time(17, 0),
            slot_interval_minutes=60,
        ))
        await db_session.flush()

        summaries = await schedule_service.get_public_slot_dates(db_session, org, days=7)
        bookable = [s for s in summaries if s.state == AvailabilityState.AVAILABLE]
        closed = [s for s in summaries if s.state == AvailabilityState.NOT_OPEN]
        assert len(bookable) == 1
        assert len(closed) == 6
        assert all(s.remaining is None for s in closed)

    async def test_holiday_exception_overrides_availability(self, db_session):
        org = await make_org(db_session, "Disc Pub Holiday")
        counter = await make_counter(db_session, org, QueueMode.SLOTS)
        await publish_hours(db_session, org)
        target = future_slot_date(days_ahead=3)
        db_session.add(ScheduleException(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            exception_date=target,
            exception_type=ScheduleExceptionType.HOLIDAY,
            reason="Founders' day",
        ))
        await db_session.flush()

        summaries = await schedule_service.get_public_slot_dates(db_session, org, days=5)
        holiday = next(s for s in summaries if s.date == target)
        assert holiday.state == AvailabilityState.HOLIDAY
        # other dates stay bookable
        assert any(
            s.state == AvailabilityState.AVAILABLE and s.remaining == 80
            for s in summaries
        )

    async def test_horizon_limits_enforced(self, db_session):
        org = await make_org(db_session, "Disc Pub Horizon")
        with pytest.raises(HTTPException) as exc:
            await schedule_service.get_public_slot_dates(db_session, org, days=0)
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            await schedule_service.get_public_slot_dates(
                db_session, org,
                days=schedule_service.MAX_ADVANCE_BOOKING_DAYS + 1,
            )
        assert exc.value.status_code == 400
        # boundary value is accepted
        summaries = await schedule_service.get_public_slot_dates(
            db_session, org, days=schedule_service.MAX_ADVANCE_BOOKING_DAYS
        )
        assert len(summaries) == schedule_service.MAX_ADVANCE_BOOKING_DAYS

    async def test_inactive_counter_slot_inventory_ignored(self, db_session):
        org = await make_org(db_session, "Disc Pub InactiveCounter")
        await make_counter(db_session, org, QueueMode.SLOTS, status=CounterStatus.INACTIVE)
        await publish_hours(db_session, org)

        summaries = await schedule_service.get_public_slot_dates(db_session, org, days=2)
        assert all(s.remaining is None for s in summaries)
        assert all(s.state == AvailabilityState.AVAILABLE for s in summaries)

    async def test_limited_state_on_low_remaining(self, db_session, monkeypatch):
        org = await make_org(db_session, "Disc Pub Limited")
        counter = await make_counter(db_session, org, QueueMode.SLOTS)
        await publish_hours(db_session, org)
        # Freeze the SCH-005 cutoff clocks at 08:30 IST of the real tenant
        # today so the day's only slot (09:00-10:00 IST) stays bookable
        # regardless of the real wall clock.
        frozen_today = get_tenant_today(org, None)
        frozen_now = datetime.combine(frozen_today, time(3, 0), tzinfo=timezone.utc)
        monkeypatch.setattr(
            schedule_service, "get_tenant_today", lambda _o, _b: frozen_today
        )
        monkeypatch.setattr(schedule_service, "_utcnow", lambda: frozen_now)
        await schedule_service.get_public_slot_dates(db_session, org, days=1)
        # Narrow to a single-slot day so the LIMITED ratio is easy to hit.
        from sqlalchemy import delete
        await db_session.execute(
            delete(WeeklySchedule).where(
                WeeklySchedule.organization_id == org.id,
                WeeklySchedule.scope_level == ScheduleScopeLevel.ORGANIZATION,
            )
        )
        await db_session.flush()
        db_session.add(WeeklySchedule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            day_of_week=get_tenant_today(org, None).weekday(),
            open_time=time(9, 0),
            close_time=time(10, 0),
            slot_interval_minutes=60,
        ))
        await db_session.flush()
        # previous slots for today still exist with stale capacity; regenerate
        await db_session.execute(
            delete(TimeSlot).where(TimeSlot.counter_id == counter.id)
        )
        await db_session.flush()
        await schedule_service.get_public_slot_dates(db_session, org, days=1)
        slots = await list_slots_for_date(
            db_session, counter.id, get_tenant_today(org, None)
        )
        assert len(slots) == 1
        slots[0].booked_count = 8  # 2 of 10 remaining → 20% < 30% → LIMITED
        await db_session.flush()

        summaries = await schedule_service.get_public_slot_dates(db_session, org, days=1)
        assert summaries[0].state == AvailabilityState.LIMITED
        assert summaries[0].remaining == 2


# ── Inactive-service booking eligibility guard (audit defect #1) ─────────────

class TestInactiveServiceGuard:
    async def test_inactive_service_day_queue_rejected(self, db_session):
        org = await make_org(db_session, "Guard DayQ Org")
        counter = await make_counter(db_session, org, QueueMode.DAY_QUEUE)
        svc = await make_service(db_session, org, "Paused Service", is_active=False)

        with pytest.raises(ValueError, match="currently unavailable"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=svc.id, user_id=None,
            )
        from sqlalchemy import func, select
        from models.token import Token
        count = (
            await db_session.execute(
                select(func.count()).select_from(Token).where(Token.counter_id == counter.id)
            )
        ).scalar()
        assert count == 0

    async def test_inactive_service_slot_rejected_without_consuming_capacity(self, db_session):
        org = await make_org(db_session, "Guard Slot Org")
        counter = await make_counter(db_session, org, QueueMode.SLOTS)
        svc = await make_service(db_session, org, "Paused Slot Service", is_active=False)
        await publish_hours(db_session, org)
        target = future_slot_date(days_ahead=3)
        await generate_slots_for_date(db_session, counter, target)
        slot = (await list_slots_for_date(db_session, counter.id, target))[0]

        with pytest.raises(ValueError, match="currently unavailable"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=svc.id, user_id=None, slot_id=slot.id,
            )
        await db_session.refresh(slot)
        assert slot.booked_count == 0

    async def test_active_service_slot_booking_still_works(self, db_session):
        org = await make_org(db_session, "Guard Active Org")
        counter = await make_counter(db_session, org, QueueMode.SLOTS)
        svc = await make_service(db_session, org, "Live Service")
        await publish_hours(db_session, org)
        target = future_slot_date(days_ahead=3)
        await generate_slots_for_date(db_session, counter, target)
        slot = (await list_slots_for_date(db_session, counter.id, target))[0]

        token = await token_service.book_token(
            db=db_session, org_id=org.id, counter_id=counter.id,
            service_id=svc.id, user_id=None, slot_id=slot.id,
        )
        assert token.slot_id == slot.id
        await db_session.refresh(slot)
        assert slot.booked_count == 1


# ── API surface (validation & honest empty/error states) ─────────────────────

class TestDiscoveryAPI:
    async def test_search_with_phase3_filters_ok(self, client):
        resp = await client.get(
            "/user/organizations/search",
            params={
                "q": "Definitely-Not-A-Real-Org",
                "service_id": 999999,
                "pincode": "999999",
                "slot_date": "2026-12-31",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["data"] == []

    async def test_search_invalid_radius_rejected(self, client):
        resp = await client.get(
            "/user/organizations/search", params={"radius_km": 0}
        )
        assert resp.status_code == 422

    async def test_search_invalid_lat_rejected(self, client):
        resp = await client.get(
            "/user/organizations/search", params={"lat": 95, "lng": 77}
        )
        assert resp.status_code == 422

    async def test_slot_dates_unknown_org_404(self, client):
        resp = await client.get("/public/organizations/999999/slot-dates")
        assert resp.status_code == 404

    async def test_slot_dates_days_out_of_range_rejected(self, client):
        resp = await client.get(
            "/public/organizations/1/slot-dates", params={"days": 61}
        )
        assert resp.status_code == 422

