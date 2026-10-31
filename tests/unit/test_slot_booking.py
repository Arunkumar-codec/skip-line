"""
tests/unit/test_slot_booking.py
───────────────────────────────
Phase 3 Task 3: atomic slot reservation integrated into the booking engine.

Covers the twelve test categories from the Task 3 brief:
successful slot booking, day-queue regression, full-slot rejection,
wrong-counter / wrong-organization slots, closed & unavailable days,
capacity-rule enforcement, token-creation-failure rollback, concurrency,
duplicate/retry semantics, legacy slot_id = NULL tokens, IntegrityError
handling and additional edge cases.

Concurrency note: these tests run on SQLite. SQLite serializes writers and
re-evaluates the guarded UPDATE against current committed data, which
proves the *application-level* guard. MySQL (InnoDB) row-locking semantics
differ in mechanism but not in the invariant; a real-MySQL integration run
remains a deployment-gate requirement and is NOT claimed here.
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from app.database import Base
from app.services import token_service
from app.services.schedule_service import (
    MAX_ADVANCE_BOOKING_DAYS,
    generate_slots_for_date,
    get_tenant_today,
    list_slots_for_date,
)
from models.capacity_rule import BookingSource, CapacityLimitType, CapacityRule
from models.counter import Counter, QueueMode
from models.organization import Organization
from models.schedule_exception import ScheduleException, ScheduleExceptionType
from models.service import Service
from models.time_slot import TimeSlot
from models.token import Token
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule
import models  # noqa: F401  — register all mappers


# ── Fixture helpers ───────────────────────────────────────────────────────────

# book_token() commits mid-test, so committed rows persist in the shared
# session-scoped database across tests (conftest's teardown rollback cannot
# undo them). Every org therefore needs a unique name/email/registration.
_org_seq = 0

async def make_org(db, name=None) -> Organization:
    global _org_seq
    _org_seq += 1
    name = name or f"Slot Book Org {_org_seq}"
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


async def make_counter(db, org, mode=QueueMode.SLOTS, branch_id=None) -> Counter:
    counter = Counter(
        organization_id=org.id,
        branch_id=branch_id,
        counter_name=f"Counter {abs(hash((org.name, mode.value))) % 10**4}",
        counter_number=abs(hash(mode.value)) % 100 + 1,
        token_prefix="S",
        queue_mode=mode,
    )
    db.add(counter)
    await db.flush()
    return counter


async def publish_hours(db, org, counter=None, opens=time(9, 0), closes=time(17, 0)):
    """Publish org-level 9-17 hours for every weekday (slot interval 60)."""
    for dow in range(7):
        db.add(WeeklySchedule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            day_of_week=dow,
            open_time=opens,
            close_time=closes,
            slot_interval_minutes=60 if counter is None or counter.queue_mode == QueueMode.SLOTS else None,
        ))
    await db.flush()


async def add_schedule_for_dow(db, org, dow):
    """Publish hours for a single weekday only (leaves others NOT_OPEN)."""
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


async def slot_for(db, counter, target: date, index: int = 0) -> TimeSlot:
    """Generate slots for `target` and return the `index`-th one."""
    await generate_slots_for_date(db, counter, target)
    slots = await list_slots_for_date(db, counter.id, target)
    assert slots, f"no slots generated for {target}"
    return slots[index]


# 2026-11-02 is a Monday (published hours every weekday → bookable)
BOOK_DAY = date(2026, 11, 2)

# ── SCH-005 same-day cutoff: frozen clocks ────────────────────────────────────
#
# The cutoff is instant-based, so the tests freeze the booking clock
# (token_service._utcnow) and the tenant-date resolution
# (token_service.get_tenant_today) to a fixed business day: 2026-11-02,
# frozen at 03:00 UTC = 08:30 Asia/Kolkata — mid-morning, so a morning slot
# has already started while an 09:15 IST slot still lies ahead. All times
# stay within 00:00-18:30 UTC on the frozen date (representable per the
# (slot_date, UTC time-of-day) storage pair).

CUT_OFF_TODAY = BOOK_DAY
_FROZEN_NOW = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)


def freeze_cutoff_clock(monkeypatch, frozen_now=None):
    frozen = frozen_now or _FROZEN_NOW
    monkeypatch.setattr(token_service, "_utcnow", lambda: frozen)
    monkeypatch.setattr(
        token_service, "get_tenant_today", lambda org, branch: CUT_OFF_TODAY
    )


# ── 1. Successful slot-based booking ─────────────────────────────────────────

class TestSuccessfulSlotBooking:
    async def test_slot_booking_creates_token_and_consumes_capacity(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)

        token = await token_service.book_token(
            db=db_session, org_id=org.id, counter_id=counter.id,
            service_id=None, user_id=None, slot_id=slot.id,
        )
        assert token.slot_id == slot.id
        assert token.token_number.startswith("S-")
        assert token.status == "waiting"
        await db_session.refresh(slot)
        assert slot.booked_count == 1

    async def test_slot_booking_without_counter_uses_slots_counter(self, db_session):
        """slot_id alone routes to the slot's own counter (tenant-checked)."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)

        token = await token_service.book_token(
            db=db_session, org_id=org.id, counter_id=None,
            service_id=None, user_id=None, slot_id=slot.id,
        )
        assert token.counter_id == counter.id
        assert token.slot_id == slot.id

    async def test_booking_fills_slot_exactly_once(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)

        for _ in range(3):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )
        await db_session.refresh(slot)
        assert slot.booked_count == 3   # exactly +1 per booking, never +2


# ── 2. Day-queue regression ──────────────────────────────────────────────────

class TestDayQueueRegression:
    async def test_day_queue_booking_unchanged(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org, mode=QueueMode.DAY_QUEUE)

        token = await token_service.book_token(
            db=db_session, org_id=org.id, counter_id=counter.id,
            service_id=None, user_id=None,
        )
        assert token.slot_id is None                      # legacy linkage
        assert token.queue_position is not None
        assert token.status == "waiting"

    async def test_day_queue_counter_rejects_slot_reference(self, db_session):
        org = await make_org(db_session)
        dq_counter = await make_counter(db_session, org, mode=QueueMode.DAY_QUEUE)
        slot_counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, slot_counter)
        slot = await slot_for(db_session, slot_counter, BOOK_DAY)

        with pytest.raises(ValueError, match="does not belong"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=dq_counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )

    async def test_reserve_rejects_day_queue_counter_directly(self, db_session):
        """Defense-in-depth: the reservation primitive refuses non-slot
        counters even if a slot row references one."""
        org = await make_org(db_session)
        dq_counter = await make_counter(db_session, org, mode=QueueMode.DAY_QUEUE)
        slot = TimeSlot(
            organization_id=org.id, counter_id=dq_counter.id, service_id=None,
            slot_date=BOOK_DAY, start_time=time(3, 30), end_time=time(4, 30),
            capacity=10,
        )
        db_session.add(slot)
        await db_session.flush()

        with pytest.raises(ValueError, match="does not support"):
            await token_service._validate_and_reserve_slot(db_session, org, dq_counter, slot)

    async def test_slot_mode_counter_requires_slot_selection(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)

        with pytest.raises(ValueError, match="appointment time slots"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None,
            )

    async def test_auto_routing_skips_slot_mode_counters(self, db_session):
        org = await make_org(db_session)
        slot_counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, slot_counter)
        service = Service(
            organization_id=org.id, name="Routed Svc",
            price=Decimal("0.00"), estimated_minutes=5,
        )
        db_session.add(service)
        await db_session.flush()
        from models.counter_service import CounterService
        db_session.add(CounterService(
            counter_id=slot_counter.id, service_id=service.id
        ))
        await db_session.flush()

        # Only a slot-mode counter serves this service → auto-routing without
        # a slot cannot book it.
        with pytest.raises(ValueError, match="No active counter"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=None,
                service_id=service.id, user_id=None,
            )


# ── 3/4. Full-slot, wrong-counter and wrong-organization rejection ────────────

class TestSlotOwnershipAndCapacityRejection:
    async def test_full_slot_rejected(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)
        slot.capacity = 1
        await db_session.flush()

        await token_service.book_token(
            db=db_session, org_id=org.id, counter_id=counter.id,
            service_id=None, user_id=None, slot_id=slot.id,
        )
        # Per-slot guard: other slots keep the day aggregate open, so the
        # guarded UPDATE itself (rowcount 0) rejects the second booking.
        with pytest.raises(ValueError, match="filled up"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )
        await db_session.refresh(slot)
        assert slot.booked_count == 1

    async def test_wrong_counter_slot_rejected(self, db_session):
        org = await make_org(db_session)
        counter_a = await make_counter(db_session, org)
        counter_b = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter_a)
        slot = await slot_for(db_session, counter_a, BOOK_DAY)

        with pytest.raises(ValueError, match="does not belong"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter_b.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )

    async def test_wrong_organization_slot_rejected(self, db_session):
        org = await make_org(db_session)
        other_org = await make_org(db_session)
        other_counter = await make_counter(db_session, other_org)
        await publish_hours(db_session, other_org, other_counter)
        foreign_slot = await slot_for(db_session, other_counter, BOOK_DAY)

        with pytest.raises(ValueError, match="not available"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=None,
                service_id=None, user_id=None, slot_id=foreign_slot.id,
            )


# ── 5. Closed / unavailable day rejection ────────────────────────────────────

class TestClosedDayRejection:
    async def test_holiday_rejects_booking(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)
        db_session.add(ScheduleException(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            exception_date=BOOK_DAY,
            exception_type=ScheduleExceptionType.HOLIDAY,
            reason="Diwali holiday",
        ))
        await db_session.flush()

        with pytest.raises(ValueError, match="Diwali"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )

    async def test_maintenance_closure_rejects_booking(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)
        db_session.add(ScheduleException(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            exception_date=BOOK_DAY,
            exception_type=ScheduleExceptionType.MAINTENANCE,
            reason="Server maintenance",
        ))
        await db_session.flush()

        with pytest.raises(ValueError, match="maintenance"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )

    async def test_not_open_weekday_rejects_booking(self, db_session):
        """Published calendar without hours for this weekday → NOT_OPEN."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        # Publish ONLY Monday hours; craft a slot manually on a Tuesday.
        await add_schedule_for_dow(db_session, org, 0)
        slot = TimeSlot(
            organization_id=org.id, counter_id=counter.id, service_id=None,
            slot_date=date(2026, 11, 3),   # Tuesday
            start_time=time(3, 30), end_time=time(4, 30),  # UTC for 9-10 IST
            capacity=10,
        )
        db_session.add(slot)
        await db_session.flush()

        with pytest.raises(ValueError, match="No published operating hours"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )

    async def test_inactive_slot_rejected(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)
        slot.is_active = False
        await db_session.flush()

        with pytest.raises(ValueError, match="no longer available"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )


# ── 6. Capacity-rule enforcement ─────────────────────────────────────────────

class TestCapacityRuleEnforcement:
    async def test_slot_capacity_rule_bound_respected(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        db_session.add(CapacityRule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.COUNTER,
            scope_id=counter.id,
            limit_type=CapacityLimitType.SLOT_CAPACITY,
            booking_source=BookingSource.ALL,
            max_count=2,
        ))
        await db_session.flush()
        slot = await slot_for(db_session, counter, BOOK_DAY)
        assert slot.capacity == 2   # rule bound the slot capacity at generation

        for _ in range(2):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )
        # Other slots keep the day aggregate open → the per-slot guarded
        # UPDATE rejects the third reservation (rule-bound capacity).
        with pytest.raises(ValueError, match="filled up"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )
        await db_session.refresh(slot)
        assert slot.booked_count == 2   # guard blocked the third reservation

    async def test_counter_daily_limit_still_applies(self, db_session):
        """Decision 13A: the existing daily quota remains a layered check."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        counter.daily_limit = 1
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)

        await token_service.book_token(
            db=db_session, org_id=org.id, counter_id=counter.id,
            service_id=None, user_id=None, slot_id=slot.id,
        )
        slot2 = await slot_for(db_session, counter, BOOK_DAY, index=1)
        with pytest.raises(ValueError, match="daily limit"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot2.id,
            )


# ── 7. Token-creation failure with correct rollback ──────────────────────────

class TestFailureRollback:
    async def test_qr_failure_rolls_back_reservation_and_token(self, db_session, monkeypatch):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)
        # Persist the setup first: book_token commits internally, so a
        # post-failure rollback must not wipe the fixture rows themselves.
        await db_session.commit()

        async def boom(**kwargs):
            raise RuntimeError("disk full")
        monkeypatch.setattr("app.utils.qr_generator.generate_token_qr", boom)

        with pytest.raises(RuntimeError):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )
        await db_session.rollback()
        await db_session.refresh(slot)
        assert slot.booked_count == 0           # reservation undone
        tokens = (await db_session.execute(
            select(Token).where(Token.slot_id == slot.id)
        )).scalars().all()
        assert tokens == []                     # no orphan token

    async def test_commit_conflict_rolls_back_reservation(self, db_session, monkeypatch):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)
        await db_session.commit()   # persist setup before simulating the conflict

        async def conflict(self):
            raise IntegrityError(
                "INSERT INTO tokens", {},
                Exception("UNIQUE constraint failed: tokens.sequence"),
            )
        monkeypatch.setattr(type(db_session), "commit", conflict)

        with pytest.raises(ValueError, match="temporary conflict"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )
        monkeypatch.undo()
        await db_session.rollback()
        await db_session.refresh(slot)
        assert slot.booked_count == 0
        tokens = (await db_session.execute(
            select(Token).where(Token.slot_id == slot.id)
        )).scalars().all()
        assert tokens == []


# ── 8. Concurrency: competing for the final available capacity ────────────────

class TestConcurrency:
    async def test_guarded_update_rejects_when_slot_exhausted(self, db_session):
        """Direct proof of the guarded UPDATE: the day still has capacity in
        ANOTHER slot (so validation passes), but the target slot itself is
        exhausted → 0 affected rows → clean rejection, no occupancy change."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        target = await slot_for(db_session, counter, BOOK_DAY, index=0)
        other = await slot_for(db_session, counter, BOOK_DAY, index=1)
        target.capacity = 1
        target.booked_count = 1
        await db_session.flush()
        await db_session.refresh(other)   # ensure aggregate sees free capacity

        with pytest.raises(ValueError, match="filled up"):
            await token_service._validate_and_reserve_slot(
                db_session, org, counter, target
            )
        await db_session.refresh(target)
        assert target.booked_count == 1
        assert other.booked_count == 0

    async def test_concurrent_bookings_never_oversell(self, tmp_path):
        """Six simultaneous requests compete for ONE place in a capacity-1
        slot on a real two-connection SQLite file database.

        Application-behavior proof on SQLite only — MySQL InnoDB locking is
        NOT exercised here (deployment-gate requirement, not claimed)."""
        db_path = Path(tmp_path) / "book_conc.sqlite3"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"timeout": 30},   # absorb writer-lock waits
        )
        SessionMaker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with SessionMaker() as setup:
                org = await make_org(setup)
                counter = await make_counter(setup, org)
                await publish_hours(setup, org, counter)
                slot = await slot_for(setup, counter, BOOK_DAY)
                slot.capacity = 1
                await setup.commit()
                org_id, counter_id, slot_id = org.id, counter.id, slot.id

            async def attempt(i: int):
                async with SessionMaker() as s:
                    try:
                        token = await token_service.book_token(
                            db=s, org_id=org_id, counter_id=counter_id,
                            service_id=None, user_id=None, slot_id=slot_id,
                        )
                        return ("ok", token.id)
                    except ValueError as e:
                        return ("rejected", str(e))

            results = await asyncio.gather(*(attempt(i) for i in range(6)))

            outcomes = [r[0] for r in results]
            assert outcomes.count("ok") == 1, results
            assert outcomes.count("rejected") == 5, results
            # every rejection is a clean domain message, not a leaked error
            for kind, detail in results:
                if kind == "rejected":
                    assert "OperationalError" not in detail
                    assert "locked" not in detail

            async with SessionMaker() as verify:
                final_slot = await verify.get(TimeSlot, slot_id)
                assert final_slot.booked_count == 1
                slots_tokens = (await verify.execute(
                    select(Token).where(Token.slot_id == slot_id)
                )).scalars().all()
                assert len(slots_tokens) == 1
        finally:
            await engine.dispose()

    async def test_retry_after_failure_books_cleanly(self, db_session, monkeypatch):
        """A failed attempt must not double-reserve on retry."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)
        await db_session.commit()   # persist setup before the failing attempt

        real_commit = type(db_session).commit
        # capture ids before rollback expires the instances
        org_id, counter_id, slot_id = org.id, counter.id, slot.id

        async def failing_commit(self):
            raise IntegrityError("INSERT", {}, Exception("boom"))
        monkeypatch.setattr(type(db_session), "commit", failing_commit)
        with pytest.raises(ValueError):
            await token_service.book_token(
                db=db_session, org_id=org_id, counter_id=counter_id,
                service_id=None, user_id=None, slot_id=slot_id,
            )
        monkeypatch.setattr(type(db_session), "commit", real_commit)
        await db_session.rollback()

        token = await token_service.book_token(
            db=db_session, org_id=org_id, counter_id=counter_id,
            service_id=None, user_id=None, slot_id=slot_id,
        )
        assert token.slot_id == slot.id
        await db_session.refresh(slot)
        assert slot.booked_count == 1   # exactly one reservation net


# ── 9/11. Duplicates, horizon edge cases and integrity conflicts ─────────────

class TestEdgeCases:
    async def test_past_slot_rejected(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        past = date.today() - timedelta(days=2)
        slot = TimeSlot(
            organization_id=org.id, counter_id=counter.id, service_id=None,
            slot_date=past, start_time=time(3, 30), end_time=time(4, 30),
            capacity=10,
        )
        db_session.add(slot)
        await db_session.flush()

        with pytest.raises(ValueError, match="in the past"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )

    async def test_beyond_horizon_slot_rejected(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        far = date.today() + timedelta(days=MAX_ADVANCE_BOOKING_DAYS + 5)
        slot = TimeSlot(
            organization_id=org.id, counter_id=counter.id, service_id=None,
            slot_date=far, start_time=time(3, 30), end_time=time(4, 30),
            capacity=10,
        )
        db_session.add(slot)
        await db_session.flush()

        with pytest.raises(ValueError, match="days in advance"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )

    async def test_same_day_boundary_is_bookable(self, db_session, monkeypatch):
        """A slot dated today (tenant tz) passes the past-date guard and is
        booked before its start instant (SCH-005 same-day cutoff)."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        freeze_cutoff_clock(monkeypatch)
        today_tenant = CUT_OFF_TODAY  # frozen tenant date (token_service patched)
        # ensure slot generation covers the frozen date (weekday hours
        # published daily); 09:00 IST (03:30 UTC) is future vs frozen 08:30 IST
        slot = await slot_for(db_session, counter, today_tenant)
        token = await token_service.book_token(
            db=db_session, org_id=org.id, counter_id=counter.id,
            service_id=None, user_id=None, slot_id=slot.id,
        )
        assert token.slot_id == slot.id
        await db_session.refresh(slot)
        assert slot.booked_count == 1

    async def test_reduced_capacity_exception_scales_slot(self, db_session):
        """Reduced capacity applies at generation; booking respects it."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        db_session.add(ScheduleException(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            exception_date=BOOK_DAY,
            exception_type=ScheduleExceptionType.REDUCED_CAPACITY,
            reason="Staff shortage",
            capacity_factor=Decimal("0.50"),
        ))
        await db_session.flush()
        slot = await slot_for(db_session, counter, BOOK_DAY)
        assert slot.capacity == 5   # 10 * 0.5

        for _ in range(5):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )
        with pytest.raises(ValueError, match="filled up"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )

    async def test_slot_row_deleted_before_update(self, db_session):
        """Slot row removed between loading and the guarded UPDATE → the
        UPDATE matches 0 rows → clean domain error instead of an
        IntegrityError leak."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        slot = await slot_for(db_session, counter, BOOK_DAY)
        slot_id = slot.id

        await db_session.execute(delete(TimeSlot).where(TimeSlot.id == slot_id))

        with pytest.raises(ValueError):
            await token_service._validate_and_reserve_slot(db_session, org, counter, slot)


# ── SCH-005: same-day cutoff (bookable until the start instant) ──────────────

class TestSameDayCutoff:
    async def test_started_same_day_slot_rejected(self, db_session, monkeypatch):
        """A slot of today whose start instant has passed is rejected even
        though its business date is still bookable; no place is consumed."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        freeze_cutoff_clock(monkeypatch)
        slot = TimeSlot(
            organization_id=org.id, counter_id=counter.id, service_id=None,
            slot_date=CUT_OFF_TODAY,
            start_time=time(2, 15), end_time=time(2, 45),  # 07:45-08:15 IST
            capacity=10,
        )
        db_session.add(slot)
        await db_session.flush()

        with pytest.raises(ValueError, match="already started"):
            await token_service.book_token(
                db=db_session, org_id=org.id, counter_id=counter.id,
                service_id=None, user_id=None, slot_id=slot.id,
            )
        await db_session.refresh(slot)
        assert slot.booked_count == 0

    async def test_upcoming_same_day_slot_bookable(self, db_session, monkeypatch):
        """A same-day slot still ahead of the frozen instant books normally."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        freeze_cutoff_clock(monkeypatch)
        slot = TimeSlot(
            organization_id=org.id, counter_id=counter.id, service_id=None,
            slot_date=CUT_OFF_TODAY,
            start_time=time(3, 45), end_time=time(4, 45),  # 09:15-10:15 IST
            capacity=10,
        )
        db_session.add(slot)
        await db_session.flush()

        token = await token_service.book_token(
            db=db_session, org_id=org.id, counter_id=counter.id,
            service_id=None, user_id=None, slot_id=slot.id,
        )
        assert token.slot_id == slot.id
        await db_session.refresh(slot)
        assert slot.booked_count == 1

    async def test_cutoff_is_instant_based_not_wall_time(self, db_session, monkeypatch):
        """A future-dated slot whose stored UTC time-of-day is earlier than
        the current UTC time-of-day remains bookable — the cutoff compares
        instants, never raw wall clocks."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await publish_hours(db_session, org, counter)
        freeze_cutoff_clock(monkeypatch)
        future = CUT_OFF_TODAY + timedelta(days=7)  # inside the 60-day horizon
        slot = TimeSlot(
            organization_id=org.id, counter_id=counter.id, service_id=None,
            slot_date=future,
            # 02:45 UTC is EARLIER on the clock face than the frozen 03:00
            # UTC instant — only a naive wall comparison would reject this.
            start_time=time(2, 45), end_time=time(3, 15),  # 08:15-08:45 IST
            capacity=10,
        )
        db_session.add(slot)
        await db_session.flush()

        token = await token_service.book_token(
            db=db_session, org_id=org.id, counter_id=counter.id,
            service_id=None, user_id=None, slot_id=slot.id,
        )
        assert token.slot_id == slot.id
