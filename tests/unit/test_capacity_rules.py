"""
tests/unit/test_capacity_rules.py
────────────────────────────────
SCH-004: capacity-rule enforcement wired into the authoritative booking
engine (`token_service.book_token`).

Covers the rule families the capacity-rule engine evaluates as
independent, layered checks with serverside issuing channels:

- DAILY_TOKEN (org / branch / counter / service scopes; ALL vs
  source-specific axis), issuance-day bucket (created_at, Decision 13A).
- WALK_IN_QUOTA (counts walk-in tokens only).
- PER_USER (authenticated users; guests carry no identity).
- PRIORITY_RESERVE (withheld places for the rule's own source inside an
  aggregate DAILY_TOKEN cap; no container → no effect).
- Source-vs-concurrency boundaries: rejected bookings consume no slot
  occupancy; max_count = 0 blocks every booking; slots keep the guarded
  atomic reservation under a daily cap.

Concurrency note (same posture as the pre-existing engine): slot capacity
stays guarded by the atomic conditional UPDATE (existing two-connection
test); DAILY_TOKEN-style rules are count-based layered checks inside the
booking transaction — bounded by the same documented `_next_sequence` /
count-check race under genuinely parallel writers (OPEN_DECISIONS §4),
so no atomicity beyond the existing architecture is claimed here.
"""

from datetime import date, time, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import token_service
from app.services.schedule_service import generate_slots_for_date, list_slots_for_date
from models.branch import Branch
from models.capacity_rule import BookingSource, CapacityLimitType, CapacityRule
from models.counter import Counter, CounterStatus, QueueMode
from models.counter_service import CounterService
from models.organization import Organization
from models.service import Service
from models.time_slot import TimeSlot
from models.user import User
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule
import models  # noqa: F401  — register all mappers

_org_seq = 0


async def make_org(db, name=None) -> Organization:
    global _org_seq
    _org_seq += 1
    name = name or f"Cap Rule Org {_org_seq}"
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


async def make_branch(db, org, name: str) -> Branch:
    branch = Branch(
        organization_id=org.id,
        name=name,
        branch_code=f"BC-{abs(hash(name)) % 10**5}",
        address="Branch Address",
        city="Test City",
        state="Test State",
        pincode="000000",
    )
    db.add(branch)
    await db.flush()
    return branch


async def make_counter(
    db, org, mode: QueueMode = QueueMode.DAY_QUEUE, branch_id=None
) -> Counter:
    counter = Counter(
        organization_id=org.id,
        branch_id=branch_id,
        counter_name=f"Counter {org.id} {mode.value}",
        counter_number=abs(hash((org.id, branch_id, mode.value))) % 90 + 1,
        token_prefix=f"C{org.id}",
        queue_mode=mode,
    )
    db.add(counter)
    await db.flush()
    return counter


async def make_service(db, org, name: str) -> Service:
    service = Service(
        organization_id=org.id, name=name, estimated_minutes=5,
        price=None, is_active=True,
    )
    db.add(service)
    await db.flush()
    return service


async def make_user(db, email: str, name: str) -> User:
    user = User(
        name=name, email=email, mobile=f"98{abs(hash(email)) % 10**8:08d}",
        password_hash="x",
    )
    db.add(user)
    await db.flush()
    return user


def add_rule(
    db, org, scope_level, scope_id, limit_type: str,
    max_count: int, booking_source: str = BookingSource.ALL.value,
) -> CapacityRule:
    rule = CapacityRule(
        organization_id=org.id,
        scope_level=scope_level,
        scope_id=scope_id,
        limit_type=limit_type,
        booking_source=booking_source,
        max_count=max_count,
    )
    db.add(rule)
    return rule


async def book(db, org, counter, **kw):
    """book_token through the shared path (walk-in source by default)."""
    return await token_service.book_token(
        db=db, org_id=org.id, counter_id=counter.id,
        service_id=kw.pop("service_id", None), user_id=kw.pop("user_id", None),
        **kw,
    )


# ── DAILY_TOKEN: aggregate + scope levels ─────────────────────────────────────

class TestDailyTokenRules:
    async def test_org_scope_counts_across_counters(self, db_session):
        org = await make_org(db_session)
        c1 = await make_counter(db_session, org)
        c2 = await make_counter(db_session, org)
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "daily_token", 2)
        await db_session.flush()

        await book(db_session, org, c1)
        await book(db_session, org, c2)
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, c1)

    async def test_counter_scope_does_not_touch_other_counters(self, db_session):
        org = await make_org(db_session)
        c1 = await make_counter(db_session, org)
        c2 = await make_counter(db_session, org)
        add_rule(db_session, org, ScheduleScopeLevel.COUNTER, c1.id,
                 "daily_token", 1)
        await db_session.flush()

        await book(db_session, org, c1)
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, c1)
        await book(db_session, org, c2)   # other counter unaffected

    async def test_branch_scope_counts_all_branch_counters(self, db_session):
        org = await make_org(db_session)
        b1 = await make_branch(db_session, org, "Main")
        b2 = await make_branch(db_session, org, "Annex")
        cs1 = await make_counter(db_session, org, branch_id=b1.id)
        cs2 = await make_counter(db_session, org, branch_id=b1.id)
        cs3 = await make_counter(db_session, org, branch_id=b2.id)
        add_rule(db_session, org, ScheduleScopeLevel.BRANCH, b1.id,
                 "daily_token", 2)
        await db_session.flush()

        await book(db_session, org, cs1)
        await book(db_session, org, cs2)
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, cs1)
        await book(db_session, org, cs3)   # other branch unaffected

    async def test_service_scope_counts_tokens_of_that_service(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        svc = await make_service(db_session, org, "Lab Draw")
        db_session.add(CounterService(
            counter_id=counter.id, service_id=svc.id
        ))
        add_rule(db_session, org, ScheduleScopeLevel.SERVICE, svc.id,
                 "daily_token", 1)
        await db_session.flush()

        await book(db_session, org, counter, service_id=svc.id)
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter, service_id=svc.id)

    async def test_zero_max_count_blocks_any_booking(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "daily_token", 0)
        await db_session.flush()

        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter)


# ── DAILY_TOKEN: source axis (server-side channel, never client input) ───────

class TestSourceSpecificRules:
    async def test_source_specific_rule_only_caps_its_source(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "daily_token", 1, booking_source="kiosk")
        await db_session.flush()

        await book(db_session, org, counter, booking_source="kiosk")
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter, booking_source="kiosk")
        # Other sources remain independent (layered checks).
        await book(db_session, org, counter, booking_source="online")

    async def test_aggregate_plus_source_rules_layer(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "daily_token", 3, booking_source="all")
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "daily_token", 1, booking_source="kiosk")
        await db_session.flush()

        await book(db_session, org, counter, booking_source="kiosk")
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter, booking_source="kiosk")
        await book(db_session, org, counter, booking_source="online")
        await book(db_session, org, counter, booking_source="online")
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter, booking_source="online")

    async def test_invalid_source_rejected_defensively(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        await db_session.flush()

        with pytest.raises(ValueError, match="Invalid booking source"):
            await book(db_session, org, counter, booking_source="superuser")


# ── WALK_IN_QUOTA ─────────────────────────────────────────────────────────────

class TestWalkInQuota:
    async def test_quota_counts_walk_ins_only(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "walk_in_quota", 1)
        await db_session.flush()

        await book(db_session, org, counter)   # default source = walk_in
        with pytest.raises(ValueError, match="walk-in quota"):
            await book(db_session, org, counter)
        # Online issuance is not part of the walk-in quota.
        await book(db_session, org, counter, booking_source="online")

    async def test_walk_in_quota_at_counter_scope(self, db_session):
        org = await make_org(db_session)
        c1 = await make_counter(db_session, org)
        c2 = await make_counter(db_session, org)
        add_rule(db_session, org, ScheduleScopeLevel.COUNTER, c1.id,
                 "walk_in_quota", 2)
        await db_session.flush()

        await book(db_session, org, c1)
        await book(db_session, org, c1)
        with pytest.raises(ValueError, match="walk-in quota"):
            await book(db_session, org, c1)
        await book(db_session, org, c2)   # other counter unaffected


# ── PER_USER ─────────────────────────────────────────────────────────────────

class TestPerUserRules:
    async def test_per_user_rule_limits_users_and_exempt_guests(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        alice = await make_user(db_session, f"alice.{org.id}@test.example", "Alice")
        bob = await make_user(db_session, f"bob.{org.id}@test.example", "Bob")
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "per_user", 1)
        await db_session.flush()

        await book(db_session, org, counter, user_id=alice.id)
        with pytest.raises(ValueError, match="booking limit"):
            await book(db_session, org, counter, user_id=alice.id)
        await book(db_session, org, counter, user_id=bob.id)
        # Guest bookings carry no user identity → not per-user-limitable.
        await book(db_session, org, counter, user_id=None)

    async def test_per_user_source_axis_filters_counted_tokens(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        carol = await make_user(db_session, f"carol.{org.id}@test.example", "Carol")
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "per_user", 1, booking_source="online")
        await db_session.flush()

        await book(db_session, org, counter, user_id=carol.id,
                   booking_source="online")
        with pytest.raises(ValueError, match="booking limit"):
            await book(db_session, org, counter, user_id=carol.id,
                       booking_source="online")
        # Same user's kiosk issuance is a different layered check.
        await book(db_session, org, counter, user_id=carol.id,
                   booking_source="kiosk")


# ── PRIORITY_RESERVE (CTR-003 source-based allocation) ───────────────────────

class TestPriorityReserve:
    async def test_reserve_withholds_places_for_its_source(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        # Aggregate cap 5; reserve 2 places for online bookings — ordinary
        # sources top out at 3, online may consume the withheld headroom.
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "daily_token", 5)
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "priority_reserve", 2, booking_source="online")
        await db_session.flush()

        for _ in range(3):
            await book(db_session, org, counter)   # walk_in (ordinary)
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter)   # 4th ordinary blocked

        await book(db_session, org, counter, booking_source="online")   # 4
        await book(db_session, org, counter, booking_source="online")   # 5
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter, booking_source="online")
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter, booking_source="kiosk")

    async def test_reserve_for_own_source_extends_ordinary_cap(self, db_session):
        """The reserve's own source reaches the FULL aggregate cap: other
        ordinary sources are withheld from the reserve first."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "daily_token", 3)
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "priority_reserve", 2, booking_source="walk_in")
        await db_session.flush()

        # online: ordinary (withheld 2 for walk_in) → cap 1
        await book(db_session, org, counter, booking_source="online")
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter, booking_source="online")
        # walk_in: own reserve → full cap 3 (2 more places)
        await book(db_session, org, counter, booking_source="walk_in")
        await book(db_session, org, counter, booking_source="walk_in")
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter, booking_source="walk_in")

    async def test_reserve_without_daily_container_is_inert(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        add_rule(db_session, org, ScheduleScopeLevel.ORGANIZATION, org.id,
                 "priority_reserve", 2, booking_source="online")
        await db_session.flush()

        for _ in range(4):   # no aggregate cap → nothing withheld
            await book(db_session, org, counter)


# ── Interaction with the atomic slot reservation ─────────────────────────────

# Fixed far-future day (2026-11-02, Monday) for slot tests: committed rows
# (book_token commits internally) must not sit on `date.today()+N` dates —
# the discovery slot-date search tests query exactly those dates and must
# not see this module's generated slots.
SLOT_DAY = date(2026, 11, 2)


class TestCapacityRulesAndSlots:
    async def test_daily_rule_rejection_consumes_no_slot_occupancy(self, db_session):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await publish_hours(db_session, org, counter)
        add_rule(db_session, org, ScheduleScopeLevel.COUNTER, counter.id,
                 "daily_token", 1)
        await db_session.flush()
        await generate_slots_for_date(db_session, counter, SLOT_DAY)
        slots = await list_slots_for_date(db_session, counter.id, SLOT_DAY)
        slot = slots[0]
        slot2 = slots[1]

        await book(db_session, org, counter, slot_id=slot.id)
        with pytest.raises(ValueError, match="Daily booking limit"):
            await book(db_session, org, counter, slot_id=slot2.id)
        await db_session.refresh(slot2)
        assert slot2.booked_count == 0   # rejection ran before the reservation

    async def test_slot_capacity_and_daily_rules_layer(self, db_session):
        """Layered independent checks: the slot guard and the daily cap
        both bind — the more restrictive outcome wins naturally."""
        org = await make_org(db_session)
        counter = await make_counter(db_session, org, mode=QueueMode.SLOTS)
        await publish_hours(db_session, org, counter)
        add_rule(db_session, org, ScheduleScopeLevel.COUNTER, counter.id,
                 "slot_capacity", 2)
        await generate_slots_for_date(db_session, counter, SLOT_DAY)
        slot = (await list_slots_for_date(db_session, counter.id, SLOT_DAY))[0]
        assert slot.capacity == 2   # rule bound the capacity at generation

        await book(db_session, org, counter, slot_id=slot.id)
        await book(db_session, org, counter, slot_id=slot.id)
        with pytest.raises(ValueError, match="filled up"):
            await book(db_session, org, counter, slot_id=slot.id)
        await db_session.refresh(slot)
        assert slot.booked_count == 2


async def publish_hours(db, org, counter=None, opens=time(9, 0), closes=time(17, 0)):
    """Org-level 9-17 hours for every weekday (slot interval 60)."""
    for dow in range(7):
        db.add(WeeklySchedule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            day_of_week=dow,
            open_time=opens,
            close_time=closes,
            slot_interval_minutes=60,
        ))
    await db.flush()
