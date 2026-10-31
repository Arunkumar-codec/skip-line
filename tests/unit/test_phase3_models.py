"""tests/unit/test_phase3_models.py — Phase 3 data-model unit tests.

Covers Task 1 scope only: model relationships, uniqueness constraints,
validation constraints, timezone defaults, nullable slot references,
legacy day-queue compatibility, and the migration 0005 chain.
"""

from datetime import date, time
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from models.capacity_rule import BookingSource, CapacityLimitType, CapacityRule
from models.branch import Branch
from models.counter import Counter, QueueMode
from models.organization import Organization
from models.schedule_exception import ScheduleException, ScheduleExceptionType
from models.time_slot import TimeSlot
from models.token import Token
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule


async def _make_org(db, name="Phase 3 Org", email=None) -> Organization:
    org = Organization(
        name=name,
        registration_number=f"REG-{abs(hash(name)) % 10**8}",
        org_type="hospital",
        address="Test Address",
        city="Test City",
        state="Test State",
        email=email or f"{name.lower().replace(' ', '')}@test.example",
        mobile="9000000000",
        password_hash="x",
    )
    db.add(org)
    await db.flush()
    return org


async def _make_counter(db, org_id, name="Counter 1") -> Counter:
    counter = Counter(
        organization_id=org_id,
        counter_name=name,
        counter_number=1,
        token_prefix="A",
    )
    db.add(counter)
    await db.flush()
    return counter


class TestTimezoneFields:
    async def test_organization_timezone_default(self, db_session):
        org = await _make_org(db_session, "TZ Default Org")
        assert org.timezone == "Asia/Kolkata"

    def test_organization_timezone_server_default(self):
        server_default = Organization.__table__.c.timezone.server_default
        assert server_default is not None
        assert server_default.arg == "Asia/Kolkata"

    async def test_branch_timezone_nullable_override(self, db_session):
        org = await _make_org(db_session, "TZ Branch Org")
        branch = Branch(
            organization_id=org.id, name="Main", branch_code="M1",
            address="Addr", city="C", state="S", pincode="560001",
        )
        db_session.add(branch)
        await db_session.flush()
        # NULL = inherit the organization timezone (SCH-009 override semantics)
        assert branch.timezone is None

        branch.timezone = "Asia/Dubai"
        await db_session.flush()
        assert branch.timezone == "Asia/Dubai"


class TestCounterQueueMode:
    async def test_existing_counter_defaults_to_day_queue(self, db_session):
        """Legacy day-queue compatibility: no queue_mode given → day_queue."""
        org = await _make_org(db_session, "Queue Mode Org")
        counter = await _make_counter(db_session, org.id)
        assert counter.queue_mode == QueueMode.DAY_QUEUE
        assert counter.queue_mode.value == "day_queue"

    async def test_counter_can_be_slot_mode(self, db_session):
        org = await _make_org(db_session, "Queue Mode Slot Org")
        counter = Counter(
            organization_id=org.id, counter_name="Appt Counter",
            counter_number=2, token_prefix="B", queue_mode=QueueMode.SLOTS,
        )
        db_session.add(counter)
        await db_session.flush()
        assert counter.queue_mode == QueueMode.SLOTS


class TestWeeklySchedule:
    async def test_org_scope_with_hours(self, db_session):
        org = await _make_org(db_session, "Sched Org")
        schedule = WeeklySchedule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            day_of_week=0,
            open_time=time(9, 0),
            close_time=time(17, 0),
            slot_interval_minutes=30,
        )
        db_session.add(schedule)
        await db_session.flush()
        assert schedule.scope_level.value == "org"
        assert schedule.is_closed is False

    async def test_uniqueness_same_scope_and_day(self, db_session):
        org = await _make_org(db_session, "Sched Unique Org")
        db_session.add(WeeklySchedule(
            organization_id=org.id, scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id, day_of_week=1,
            open_time=time(9, 0), close_time=time(17, 0),
        ))
        await db_session.flush()
        db_session.add(WeeklySchedule(
            organization_id=org.id, scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id, day_of_week=1,
            open_time=time(10, 0), close_time=time(18, 0),
        ))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_same_scope_values_across_tenants_coexist(self, db_session):
        """Tenant-aware uniqueness: two orgs' org-level rows (scope_id = each
        org's own PK) must NOT collide — exercises organization_id in the
        unique constraint."""
        org_a = await _make_org(db_session, "Sched Tenant A")
        org_b = await _make_org(db_session, "Sched Tenant B")
        assert org_a.id != org_b.id
        for org in (org_a, org_b):
            db_session.add(WeeklySchedule(
                organization_id=org.id, scope_level=ScheduleScopeLevel.ORGANIZATION,
                scope_id=org.id, day_of_week=3,
                open_time=time(9, 0), close_time=time(17, 0),
            ))
        await db_session.flush()  # both survive → no cross-tenant collision

    async def test_day_of_week_range_check(self, db_session):
        org = await _make_org(db_session, "Sched DOW Org")
        db_session.add(WeeklySchedule(
            organization_id=org.id, scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id, day_of_week=7,
            open_time=time(9, 0), close_time=time(17, 0),
        ))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_precedence_levels_coexist_for_same_scope_id(self, db_session):
        """service > counter > branch > org precedence is resolvable only if
        rows at every scope level can exist simultaneously (SCH-002)."""
        org = await _make_org(db_session, "Sched Precedence Org")
        counter = await _make_counter(db_session, org.id)
        service_id = 999  # scope_id is polymorphic; no FK required
        for level in ScheduleScopeLevel:
            db_session.add(WeeklySchedule(
                organization_id=org.id, scope_level=level,
                scope_id=org.id, day_of_week=2,
                open_time=time(9, 0), close_time=time(17, 0),
            ))
        await db_session.flush()
        assert (
            await db_session.execute(text(
                "SELECT COUNT(*) FROM weekly_schedules WHERE scope_id = :sid "
                "AND organization_id = :oid AND day_of_week = 2"
            ), {"sid": org.id, "oid": org.id})
        ).scalar() == len(ScheduleScopeLevel)

    async def test_closed_day_without_hours(self, db_session):
        org = await _make_org(db_session, "Sched Closed Org")
        schedule = WeeklySchedule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            day_of_week=6,
            is_closed=True,
        )
        db_session.add(schedule)
        await db_session.flush()
        assert schedule.open_time is None
        assert schedule.close_time is None


class TestScheduleException:
    async def test_holiday_with_user_facing_reason(self, db_session):
        org = await _make_org(db_session, "Exc Org")
        exc = ScheduleException(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            exception_date=date(2026, 11, 1),
            exception_type=ScheduleExceptionType.HOLIDAY,
            reason="Diwali holiday",
        )
        db_session.add(exc)
        await db_session.flush()
        assert exc.exception_type.value == "holiday"
        assert exc.reason == "Diwali holiday"

    async def test_uniqueness_same_scope_and_date(self, db_session):
        org = await _make_org(db_session, "Exc Unique Org")
        db_session.add(ScheduleException(
            organization_id=org.id, scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id, exception_date=date(2026, 11, 2),
            exception_type=ScheduleExceptionType.HOLIDAY, reason="First",
        ))
        await db_session.flush()
        db_session.add(ScheduleException(
            organization_id=org.id, scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id, exception_date=date(2026, 11, 2),
            exception_type=ScheduleExceptionType.MAINTENANCE, reason="Second",
        ))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_same_scope_and_date_across_tenants_coexist(self, db_session):
        """Tenant-aware uniqueness: same scope values + same date in two
        organizations must NOT collide."""
        org_a = await _make_org(db_session, "Exc Tenant A")
        org_b = await _make_org(db_session, "Exc Tenant B")
        for org in (org_a, org_b):
            db_session.add(ScheduleException(
                organization_id=org.id, scope_level=ScheduleScopeLevel.ORGANIZATION,
                scope_id=org.id, exception_date=date(2026, 11, 9),
                exception_type=ScheduleExceptionType.HOLIDAY,
                reason="Org-specific holiday",
            ))
        await db_session.flush()  # both survive → no cross-tenant collision

    async def test_capacity_factor_check(self, db_session):
        org = await _make_org(db_session, "Exc Factor Org")
        db_session.add(ScheduleException(
            organization_id=org.id, scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id, exception_date=date(2026, 11, 3),
            exception_type=ScheduleExceptionType.REDUCED_CAPACITY,
            reason="Half day", capacity_factor=Decimal("1.50"),
        ))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    def test_all_requirement_exception_types_present(self):
        # SCH-003: holiday, emergency closure, maintenance, special hours,
        # reduced capacity, service/counter closure
        expected = {
            "holiday", "emergency_closure", "maintenance", "special_hours",
            "reduced_capacity", "counter_closure", "service_closure",
        }
        assert {t.value for t in ScheduleExceptionType} == expected


class TestTimeSlot:
    async def test_defaults_and_counter_relationship(self, db_session):
        org = await _make_org(db_session, "Slot Org")
        counter = await _make_counter(db_session, org.id)
        slot = TimeSlot(
            organization_id=org.id,
            counter_id=counter.id,
            slot_date=date(2026, 11, 4),
            start_time=time(9, 0),
            end_time=time(9, 30),
            capacity=5,
        )
        db_session.add(slot)
        await db_session.flush()
        assert slot.booked_count == 0
        assert slot.held_count == 0
        assert slot.is_active is True
        assert slot.counter.id == counter.id
        assert slot.counter_id == counter.id

    async def test_uniqueness_counter_date_start(self, db_session):
        org = await _make_org(db_session, "Slot Unique Org")
        counter = await _make_counter(db_session, org.id)
        db_session.add(TimeSlot(
            organization_id=org.id, counter_id=counter.id,
            slot_date=date(2026, 11, 5), start_time=time(9, 0),
            end_time=time(9, 30), capacity=5,
        ))
        await db_session.flush()
        db_session.add(TimeSlot(
            organization_id=org.id, counter_id=counter.id,
            slot_date=date(2026, 11, 5), start_time=time(9, 0),
            end_time=time(9, 30), capacity=5,
        ))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_within_capacity_check(self, db_session):
        org = await _make_org(db_session, "Slot Cap Org")
        counter = await _make_counter(db_session, org.id)
        db_session.add(TimeSlot(
            organization_id=org.id, counter_id=counter.id,
            slot_date=date(2026, 11, 6), start_time=time(10, 0),
            end_time=time(10, 30), capacity=5,
            booked_count=6, held_count=0,
        ))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_booked_plus_held_within_capacity_check(self, db_session):
        org = await _make_org(db_session, "Slot Held Cap Org")
        counter = await _make_counter(db_session, org.id)
        db_session.add(TimeSlot(
            organization_id=org.id, counter_id=counter.id,
            slot_date=date(2026, 11, 7), start_time=time(11, 0),
            end_time=time(11, 30), capacity=5,
            booked_count=3, held_count=3,
        ))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestCapacityRule:
    async def test_defaults_and_uniqueness(self, db_session):
        org = await _make_org(db_session, "Rule Org")
        rule = CapacityRule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.COUNTER,
            scope_id=1,
            limit_type=CapacityLimitType.WALK_IN_QUOTA,
            max_count=50,
        )
        db_session.add(rule)
        await db_session.flush()
        # 'all' is a stored value, never NULL (portable uniqueness)
        assert rule.booking_source.value == "all"
        assert rule.is_active is True

        db_session.add(CapacityRule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.COUNTER,
            scope_id=1,
            limit_type=CapacityLimitType.WALK_IN_QUOTA,
            booking_source=BookingSource.ALL,
            max_count=60,
        ))
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_same_type_different_source_coexists(self, db_session):
        org = await _make_org(db_session, "Rule Source Org")
        db_session.add(CapacityRule(
            organization_id=org.id, scope_level=ScheduleScopeLevel.COUNTER,
            scope_id=1, limit_type=CapacityLimitType.DAILY_TOKEN,
            booking_source=BookingSource.ALL, max_count=200,
        ))
        db_session.add(CapacityRule(
            organization_id=org.id, scope_level=ScheduleScopeLevel.COUNTER,
            scope_id=1, limit_type=CapacityLimitType.DAILY_TOKEN,
            booking_source=BookingSource.ONLINE, max_count=100,
        ))
        await db_session.flush()
        assert True  # flush succeeded → coexistence proven

    async def test_same_scope_type_source_across_tenants_coexist(self, db_session):
        org_a = await _make_org(db_session, "Rule Tenant A")
        org_b = await _make_org(db_session, "Rule Tenant B")
        for org in (org_a, org_b):
            db_session.add(CapacityRule(
                organization_id=org.id, scope_level=ScheduleScopeLevel.ORGANIZATION,
                scope_id=org.id, limit_type=CapacityLimitType.DAILY_TOKEN,
                booking_source=BookingSource.ALL, max_count=100,
            ))
        await db_session.flush()  # both survive → no cross-tenant collision

    def test_limit_types_cover_sch_004(self):
        expected = {
            "daily_token", "slot_capacity", "walk_in_quota",
            "per_user", "priority_reserve",
        }
        assert {t.value for t in CapacityLimitType} == expected

    def test_booking_sources_cover_ctr_003(self):
        expected = {"all", "online", "kiosk", "walk_in", "employee"}
        assert {s.value for s in BookingSource} == expected


class TestTokenSlotReference:
    async def test_legacy_day_queue_token_has_no_slot(self, db_session):
        """Existing day-queue flows must remain valid with slot_id = NULL."""
        org = await _make_org(db_session, "Token Legacy Org")
        counter = await _make_counter(db_session, org.id)
        token = Token(
            organization_id=org.id, counter_id=counter.id,
            token_number="A-001", sequence=1,
        )
        db_session.add(token)
        await db_session.flush()
        assert token.slot_id is None
        assert token.slot is None

    async def test_token_slot_link(self, db_session):
        org = await _make_org(db_session, "Token Slot Org")
        counter = await _make_counter(db_session, org.id)
        slot = TimeSlot(
            organization_id=org.id, counter_id=counter.id,
            slot_date=date(2026, 11, 8), start_time=time(9, 0),
            end_time=time(9, 30), capacity=5,
        )
        db_session.add(slot)
        await db_session.flush()

        token = Token(
            organization_id=org.id, counter_id=counter.id, slot_id=slot.id,
            token_number="A-002", sequence=2,
        )
        db_session.add(token)
        await db_session.flush()

        assert token.slot_id == slot.id
        assert token.slot.id == slot.id
        await db_session.refresh(slot)
        assert any(t.id == token.id for t in slot.tokens)


class TestMigrationChain:
    """Runs migration 0005 against a scratch SQLite file DB representing the
    0004-era schema (legacy tables built from model metadata, then stamped
    '0004').

    Note: migration 0004 itself cannot execute on SQLite (its
    batch_alter_table recreation trips over unnamed constraints), so the
    full raw chain 0001-0005 is only runnable on MySQL. This test still
    validates 0005's revision linkage with 0004, its upgrade/downgrade SQL,
    and FK ordering assumptions. MySQL execution remains an outstanding
    deployment-gate requirement."""

    PHASE3_TABLES = {
        "weekly_schedules", "schedule_exceptions", "time_slots", "capacity_rules",
    }

    def test_upgrade_to_head_and_downgrade_to_0004(self, monkeypatch, tmp_path):
        import asyncio
        from pathlib import Path

        import aiosqlite
        from alembic import command
        from alembic.config import Config
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.config import Settings

        db_file = tmp_path / "migration_test.db"
        url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
        # Patch BOTH URL properties: migrations/env.py builds its Alembic
        # URL from settings.async_database_url (it must, to use the async
        # aiomysql engine); patching only sync_database_url would leave the
        # real configured MySQL database as the migration target.
        monkeypatch.setattr(
            Settings, "sync_database_url", property(lambda self: url)
        )
        monkeypatch.setattr(
            Settings, "async_database_url", property(lambda self: url)
        )

        project_root = Path(__file__).resolve().parents[2]
        cfg = Config()
        cfg.set_main_option("script_location", str(project_root / "migrations"))

        async def build_0004_schema():
            # Minimal 0004-era parent tables — tables/columns from migrations 0001-0004
            # that migrations 0005-0011 touch or reference.
            engine = create_async_engine(url)
            async with engine.begin() as conn:
                for ddl in (
                    "CREATE TABLE organizations (id INTEGER PRIMARY KEY)",
                    "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER)",
                    "CREATE TABLE employees (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER)",
                    "CREATE TABLE counters (id INTEGER PRIMARY KEY, organization_id INTEGER)",
                    "CREATE TABLE services (id INTEGER PRIMARY KEY, organization_id INTEGER)",
                    "CREATE TABLE tokens (id INTEGER PRIMARY KEY, organization_id INTEGER, counter_id INTEGER)",
                    "CREATE TABLE payments (id INTEGER PRIMARY KEY, token_id INTEGER, organization_id INTEGER, amount NUMERIC(10, 2), payment_method VARCHAR(30), status VARCHAR(30))",
                ):
                    await conn.execute(text(ddl))
            await engine.dispose()

        asyncio.run(build_0004_schema())

        # Declare the DB at revision 0004, then run only migration 0005
        command.stamp(cfg, "0004")
        command.upgrade(cfg, "head")

        async def fetch_schema():
            async with aiosqlite.connect(str(db_file)) as conn:
                rows = await conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                return {r[0] for r in await rows.fetchall()}

        tables = asyncio.run(fetch_schema())
        for expected in (
            "weekly_schedules", "schedule_exceptions",
            "time_slots", "capacity_rules", "tokens",
        ):
            assert expected in tables

        async def fetch_columns():
            async with aiosqlite.connect(str(db_file)) as conn:
                org_sql = await (await conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='organizations'"
                )).fetchone()
                counter_sql = await (await conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='counters'"
                )).fetchone()
                token_sql = await (await conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='tokens'"
                )).fetchone()
                return org_sql[0], counter_sql[0], token_sql[0]

        org_sql, counter_sql, token_sql = asyncio.run(fetch_columns())
        assert "timezone" in org_sql
        assert "Asia/Kolkata" in org_sql
        assert "day_queue" in counter_sql
        assert "slot_id" in token_sql

        command.downgrade(cfg, "0004")

        async def fetch_after_downgrade():
            async with aiosqlite.connect(str(db_file)) as conn:
                rows = await conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                return {r[0] for r in await rows.fetchall()}

        tables_after = asyncio.run(fetch_after_downgrade())
        for removed in self.PHASE3_TABLES:
            assert removed not in tables_after
        # Legacy data-carrying tables must survive the downgrade
        assert "tokens" in tables_after

        # Re-upgrade must work cleanly (revision linkage is intact)
        command.upgrade(cfg, "head")
        tables_again = asyncio.run(fetch_after_downgrade())
        assert self.PHASE3_TABLES.issubset(tables_again)
