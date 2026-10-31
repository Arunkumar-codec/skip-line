"""
tests/unit/test_schedule_change_impact.py
─────────────────────────────────────────
SCH-007 schedule-change impact workflow, service level:

- preview validates with the CRUD cores and computes impact through the ONE
  resolution engine via shadow overlays — no writes to time_slots/config,
- resolve records explicit per-booking decisions (no execution),
- confirm re-verifies the impact digest, claims the row conditionally and
  applies config + executes decisions + notifies + audits in ONE commit,
- Decision 16 boundary: confirmation never releases slot capacity
  (booked_count stays consumed; reschedule decisions are recorded but
  blocked), Decision 13A daily quota is untouched,
- stale/expired/missing-decision guards, rejection and tenant isolation.

Services commit, so every test uses its own freshly created organization.
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import schedule_change_service as change_service
from app.services import schedule_service
from models.capacity_rule import CapacityLimitType, CapacityRule
from models.counter import Counter, CounterStatus, QueueMode
from models.notification import Notification, NotificationType
from models.organization import Organization
from models.schedule_change import ScheduleChangeEntity, ScheduleChangeStatus
from models.time_slot import TimeSlot
from models.token import Token, TokenStatus
from models.user import User
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule
import models  # noqa: F401  — register all mappers

_seq = 0
_TZ_KOLKATA = "Asia/Kolkata"


async def make_org(db: AsyncSession) -> Organization:
    global _seq
    _seq += 1
    org = Organization(
        name=f"Change Org {_seq}",
        registration_number=f"REG-SC-{_seq:05d}-{abs(hash(f'sc{_seq}')) % 10**6}",
        org_type="hospital",
        address="1 Change Way",
        city="Change City",
        state="Change State",
        email=f"schchange{_seq}@test.example",
        mobile="9100000000",
        password_hash="x",
        status="approved",
        timezone=_TZ_KOLKATA,
    )
    db.add(org)
    await db.flush()
    return org


async def make_slots_counter(db: AsyncSession, org: Organization) -> Counter:
    counter = Counter(
        organization_id=org.id,
        counter_name=f"Slots Counter {org.id}",
        counter_number=abs(hash(org.name)) % 100 + 1,
        token_prefix="S",
        queue_mode=QueueMode.SLOTS,
        status=CounterStatus.ACTIVE,
        branch_id=None,
    )
    db.add(counter)
    await db.flush()
    return counter


async def make_user(db: AsyncSession, idx: int) -> User:
    user = User(
        name=f"User {idx}",
        email=f"sch-user-{_seq}-{idx}@test.example",
        mobile=f"93000000{idx:02d}",
        password_hash="x",
    )
    db.add(user)
    await db.flush()
    return user


async def make_slot_with_booking(
    db: AsyncSession,
    org: Organization,
    counter: Counter,
    slot_date: dt.date,
    start_local: tuple[int, int],
    users: list[User],
    capacity: int = 4,
) -> TimeSlot:
    """Existing generated slot with confirmed bookings (created directly,
    matching what the booking engine would have produced)."""
    start_local_time = dt.time(start_local[0], start_local[1])
    tz = schedule_service._zone(_TZ_KOLKATA)
    start_utc = schedule_service.utc_slot_time(slot_date, start_local_time, _TZ_KOLKATA)
    end_dt = dt.datetime.combine(slot_date, start_local_time) + dt.timedelta(minutes=30)
    end_utc = schedule_service.utc_slot_time(
        slot_date, end_dt.time(), _TZ_KOLKATA
    )
    assert start_utc is not None and end_utc is not None
    slot = TimeSlot(
        organization_id=org.id,
        counter_id=counter.id,
        service_id=counter.service_id,
        slot_date=slot_date,
        start_time=start_utc,
        end_time=end_utc,
        capacity=capacity,
        booked_count=len(users),
    )
    db.add(slot)
    await db.flush()
    for seq, user in enumerate(users, start=1):
        db.add(Token(
            organization_id=org.id,
            user_id=user.id,
            counter_id=counter.id,
            slot_id=slot.id,
            token_number=f"S-{seq:03d}",
            sequence=seq,
            status=TokenStatus.WAITING,
            queue_position=seq,
            booked_at=dt.datetime.now(dt.timezone.utc),
        ))
    await db.flush()
    return slot


async def make_weekly_row(db, org, counter, dow=0):
    return await schedule_service.create_weekly_schedule(db, org.id, {
        "scope_level": "counter", "scope_id": counter.id, "day_of_week": dow,
        "open_time": dt.time(9, 0), "close_time": dt.time(12, 0),
        "slot_interval_minutes": 30,
    })


def weekly_create_data(scope_level: str, scope_id: int, dow: int = 0) -> dict:
    return {
        "entity": "weekly_schedule",
        "edit_type": "create",
        "data": {
            "scope_level": scope_level,
            "scope_id": scope_id,
            "day_of_week": dow,
            "open_time": "09:30:00",
            "close_time": "11:30:00",
            "slot_interval_minutes": 30,
        },
    }


# ── Preview impact ────────────────────────────────────────────────────────────

class TestPreviewImpact:
    async def test_preview_weekly_create_no_writes(self, db_session):
        org = await make_org(db_session)
        counter = await make_slots_counter(db_session, org)
        existing = await make_weekly_row(db_session, org, counter, dow=0)
        n_slots_before = list(
            (await db_session.execute(
                select(TimeSlot).where(TimeSlot.counter_id == counter.id)
            )).scalars()
        )

        preview = await change_service.create_preview(
            db_session, org, weekly_create_data("counter", counter.id, dow=1),
            created_by="org@example.com",
        )
        assert preview.status == ScheduleChangeStatus.PENDING
        assert preview.entity == ScheduleChangeEntity.WEEKLY_SCHEDULE
        assert preview.confirmed_at is None

        # Nothing was written anywhere (Decision 11: preview is read-only).
        slots_after = list(
            (await db_session.execute(
                select(TimeSlot).where(TimeSlot.counter_id == counter.id)
            )).scalars()
        )
        assert len(slots_after) == len(n_slots_before)
        rows_after = list(
            (await db_session.execute(
                select(WeeklySchedule).where(WeeklySchedule.organization_id == org.id)
            )).scalars()
        )
        assert rows_after == [existing]

        impact = preview.impact
        assert impact["edit_type"] == "create"
        # Proposed shadow row applies only to dow=1 days; the committed
        # dow=0 row (09:00-12:00, 6 slots) stays in force for its weekdays.
        dow1 = [
            d for d in impact["counters"][0]["days"]
            if dt.date.fromisoformat(d["date"]).weekday() == 1
        ]
        assert dow1 and all(d["state"] in ("available", "limited") for d in dow1)
        assert all(len(d["proposed_slot_times"]) == 4 for d in dow1)  # 09:30→11:30 @ 30min
        dow0 = [
            d for d in impact["counters"][0]["days"]
            if dt.date.fromisoformat(d["date"]).weekday() == 0
        ]
        assert dow0 and all(len(d["proposed_slot_times"]) == 6 for d in dow0)

    async def test_overlay_shadow_row_wins_in_resolution(self, db_session):
        """The same resolution engine accepts shadow overlays: a proposed
        closed row at counter scope wins over the committed row without any
        write (one engine, no resolution fork for previews)."""
        org = await make_org(db_session)
        counter = await make_slots_counter(db_session, org)
        await make_weekly_row(db_session, org, counter, dow=2)
        today = schedule_service.get_tenant_today(org, None)
        offset = (2 - today.weekday()) % 7
        target_date = today + dt.timedelta(days=offset or 7)
        assert target_date.weekday() == 2

        base = await schedule_service.resolve_day_availability(
            db_session, org, target_date, counter=counter
        )
        assert base.state == schedule_service.AvailabilityState.AVAILABLE

        overlay = {
            ("counter", counter.id, 2): schedule_service.ShadowScheduleRow(is_closed=True)
        }
        shadowed = await schedule_service.resolve_day_availability(
            db_session, org, target_date, counter=counter, schedule_overlay=overlay
        )
        assert shadowed.state == schedule_service.AvailabilityState.CLOSED

        # The database row was never modified.
        refetched = await schedule_service.resolve_day_availability(
            db_session, org, target_date, counter=counter
        )
        assert refetched.state == schedule_service.AvailabilityState.AVAILABLE

    async def test_delete_with_unknown_target_is_404_contract(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(ValueError, match="not found"):
            await change_service.create_preview(db_session, org, {
                "entity": "weekly_schedule", "edit_type": "delete",
                "target_id": 999999, "data": {},
            })

    async def test_occupancy_conflict_on_capacity_reduction(self, db_session):
        org = await make_org(db_session)
        counter = await make_slots_counter(db_session, org)
        user = await make_user(db_session, 1)
        today = schedule_service.get_tenant_today(org, None)
        slot_date = today + dt.timedelta(days=2)
        slot = await make_slot_with_booking(
            db_session, org, counter, slot_date,
            (9, 0), [user], capacity=4,
        )
        assert slot.booked_count == 1
        # Hours for the slot's weekday so the projector computes a would-be
        # per-slot capacity at all.
        await make_weekly_row(db_session, org, counter, dow=slot_date.weekday())

        preview = await change_service.create_preview(db_session, org, {
            "entity": "capacity_rule", "edit_type": "create",
            "data": {
                "scope_level": "counter", "scope_id": counter.id,
                "limit_type": "slot_capacity", "max_count": 0,
            },
        })
        conflicts = preview.impact["occupancy_conflicts"]
        assert conflicts, "proposed capacity below existing occupancy is a conflict"
        assert any(
            c["slot_id"] == slot.id and c["occupancy"] == 1
            and c["proposed_capacity"] == 0
            for c in conflicts
        )


# ── Confirmation ──────────────────────────────────────────────────────────────

class TestConfirmation:
    async def test_confirm_weekly_create_applies_config(self, db_session):
        org = await make_org(db_session)
        counter = await make_slots_counter(db_session, org)
        before = list(
            (await db_session.execute(
                select(WeeklySchedule).where(WeeklySchedule.organization_id == org.id)
            )).scalars()
        )

        preview = await change_service.create_preview(
            db_session, org, weekly_create_data("counter", counter.id, dow=3)
        )
        confirmed = await change_service.confirm_preview(
            db_session, org.id, preview.id, confirmed_by="org@example.com"
        )
        assert confirmed.status == ScheduleChangeStatus.CONFIRMED
        assert confirmed.confirmed_at is not None
        assert confirmed.applied_summary["config"]["created"]

        rows = [
            r for r in (
                await db_session.execute(
                    select(WeeklySchedule).where(WeeklySchedule.organization_id == org.id)
                )
            ).scalars()
        ]
        assert len(rows) == len(before) + 1
        new_row = [r for r in rows if r.day_of_week == 3]
        assert new_row and new_row[0].open_time.hour == 9 and new_row[0].open_time.minute == 30

        # No time slots materialized (Decision 17: config CRUD never generates
        # or rewrites slots; future lazily generated slots would simply exist).
        slots = list((await db_session.execute(select(TimeSlot))).scalars())
        assert not [s for s in slots if s.counter_id == counter.id]

    async def test_missing_decisions_block_confirm(self, db_session):
        org = await make_org(db_session)
        counter = await make_slots_counter(db_session, org)
        user = await make_user(db_session, 1)
        today = schedule_service.get_tenant_today(org, None)
        await make_slot_with_booking(
            db_session, org, counter, today + dt.timedelta(days=1),
            (9, 0), [user], capacity=4,
        )
        preview = await change_service.create_preview(db_session, org, {
            "entity": "capacity_rule", "edit_type": "create",
            "data": {
                "scope_level": "counter", "scope_id": counter.id,
                "limit_type": "slot_capacity", "max_count": 2,
            },
        })
        assert preview.impact["summary"]["bookings_affected"] == 1
        with pytest.raises(ValueError, match="missing resolution decisions"):
            await change_service.confirm_preview(db_session, org.id, preview.id)

        # The rule must NOT have been applied by the blocked attempt.
        rules = list((await db_session.execute(select(CapacityRule))).scalars())
        assert not [r for r in rules if r.organization_id == org.id]

    async def test_stale_digest_blocks_confirm(self, db_session):
        org = await make_org(db_session)
        counter = await make_slots_counter(db_session, org)
        await make_weekly_row(db_session, org, counter, dow=0)
        preview = await change_service.create_preview(
            db_session, org, weekly_create_data("counter", counter.id, dow=1)
        )
        # Background drift: config state mutates after the preview.
        await schedule_service.create_weekly_schedule(db_session, org.id, {
            "scope_level": "counter", "scope_id": counter.id, "day_of_week": 5,
            "open_time": dt.time(8, 0), "close_time": dt.time(9, 0),
        })
        with pytest.raises(ValueError, match="stale"):
            await change_service.confirm_preview(db_session, org.id, preview.id)
        refetched = await change_service.get_preview(db_session, org.id, preview.id)
        assert refetched.status == ScheduleChangeStatus.STALE

    async def test_expired_preview_cannot_confirm(self, db_session):
        org = await make_org(db_session)
        counter = await make_slots_counter(db_session, org)
        preview = await change_service.create_preview(
            db_session, org, weekly_create_data("counter", counter.id, dow=4)
        )
        preview.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        await db_session.commit()
        with pytest.raises(ValueError, match="expired"):
            await change_service.confirm_preview(db_session, org.id, preview.id)
        refetched = await change_service.get_preview(db_session, org.id, preview.id)
        assert refetched.status == ScheduleChangeStatus.EXPIRED

    async def test_reject_then_final(self, db_session):
        org = await make_org(db_session)
        preview = await change_service.create_preview(
            db_session, org, weekly_create_data("org", org.id, dow=6)
        )
        rejected = await change_service.reject_preview(
            db_session, org.id, preview.id, rejected_by="org@example.com"
        )
        assert rejected.status == ScheduleChangeStatus.REJECTED
        with pytest.raises(ValueError, match="already rejected"):
            await change_service.confirm_preview(db_session, org.id, preview.id)
        rows = list(
            (await db_session.execute(select(WeeklySchedule))).scalars()
        )
        assert not [r for r in rows if r.organization_id == org.id]

    async def test_double_confirm_rejected(self, db_session):
        org = await make_org(db_session)
        preview = await change_service.create_preview(
            db_session, org, weekly_create_data("org", org.id, dow=5)
        )
        await change_service.confirm_preview(db_session, org.id, preview.id)
        with pytest.raises(ValueError, match="already confirmed"):
            await change_service.confirm_preview(db_session, org.id, preview.id)

    async def test_cross_tenant_isolation(self, db_session):
        org = await make_org(db_session)
        other = await make_org(db_session)
        preview = await change_service.create_preview(
            db_session, org, weekly_create_data("org", org.id, dow=2)
        )
        with pytest.raises(ValueError, match="not found"):
            await change_service.get_preview(db_session, other.id, preview.id)
        with pytest.raises(ValueError, match="not found"):
            await change_service.confirm_preview(db_session, other.id, preview.id)


# ── Decision execution (Decision 16 boundary) ────────────────────────────────

class TestDecisionExecution:
    async def test_cancel_honor_reschedule_outcomes(self, db_session):
        org = await make_org(db_session)
        counter = await make_slots_counter(db_session, org)
        u1, u2, u3 = await make_user(db_session, 1), await make_user(db_session, 2), await make_user(db_session, 3)
        today = schedule_service.get_tenant_today(org, None)
        tomorrow = today + dt.timedelta(days=1)
        slot = await make_slot_with_booking(
            db_session, org, counter, tomorrow, (9, 0), [u1, u2, u3], capacity=4,
        )
        tokens = {
            t.user_id: t for t in
            (await db_session.execute(
                select(Token).where(Token.counter_id == counter.id)
            )).scalars()
        }
        t1, t2, t3 = tokens[u1.id], tokens[u2.id], tokens[u3.id]
        assert slot.booked_count == 3

        # Proposal: counter-level weekly UPDATE collapses the opening hours.
        row = await make_weekly_row(db_session, org, counter, dow=tomorrow.weekday())
        preview = await change_service.create_preview(db_session, org, {
            "entity": "weekly_schedule", "edit_type": "update",
            "target_id": row.id,
            "data": {"open_time": "10:00:00", "close_time": "10:30:00"},
        })
        affected = {b["token_id"] for b in preview.impact["affected_bookings"]}
        assert {t1.id, t2.id, t3.id} <= affected

        preview = await change_service.record_decisions(
            db_session, org.id, preview.id,
            {
                str(t1.id): {"decision": "cancel", "reason": "arrive later"},
                str(t2.id): {"decision": "honor"},
                str(t3.id): {"decision": "reschedule", "reason": "wants later"},
            },
            resolved_by="org@example.com",
        )
        confirmed = await change_service.confirm_preview(db_session, org.id, preview.id)

        # Statuses + Decision 16: slot capacity is never released.
        refreshed = await db_session.get(Token, t1.id)
        assert refreshed.status == TokenStatus.CANCELLED
        await db_session.refresh(slot)
        assert slot.booked_count == 3  # cancelled places stay consumed
        assert (await db_session.get(Token, t2.id)).status == TokenStatus.WAITING
        assert (await db_session.get(Token, t3.id)).status == TokenStatus.WAITING

        summary = confirmed.applied_summary["decisions"]
        assert summary["canceled"] == 1
        assert summary["honored"] == 1
        assert summary["reschedules_blocked"] == 1
        assert summary["notified"] == 1  # confirmed bookings: only u1 has a notification

        notifs = list((await db_session.execute(
            select(Notification).where(Notification.token_id == t1.id)
        )).scalars())
        assert len(notifs) == 1 and notifs[0].user_id == u1.id
        assert notifs[0].type == NotificationType.SCHEDULE_CHANGE

        assert confirmed.decisions[str(t3.id)]["outcome"] == "reschedule_blocked"
        assert "Decision 16" in confirmed.decisions[str(t3.id)]["outcome_detail"]
        assert confirmed.decisions[str(t1.id)]["outcome"] == "cancelled"
        assert confirmed.decisions[str(t2.id)]["outcome"] == "honor_confirmed"

    async def test_daily_quota_never_released_on_cancel(self, db_session):
        """Decision 13A: cancellation (manual or schedule-driven) never
        credits daily quota back — the cancelled token stays cancelled and
        the slot place stays consumed."""
        org = await make_org(db_session)
        counter = await make_slots_counter(db_session, org)
        user = await make_user(db_session, 1)
        today = schedule_service.get_tenant_today(org, None)
        slot = await make_slot_with_booking(
            db_session, org, counter, today + dt.timedelta(days=1),
            (9, 0), [user], capacity=2,
        )
        token = (await db_session.execute(
            select(Token).where(Token.counter_id == counter.id)
        )).scalars().first()
        preview = await change_service.create_preview(db_session, org, {
            "entity": "capacity_rule", "edit_type": "create",
            "data": {
                "scope_level": "counter", "scope_id": counter.id,
                "limit_type": "slot_capacity", "max_count": 2,
            },
        })
        preview = await change_service.record_decisions(
            db_session, org.id, preview.id,
            {str(token.id): {"decision": "cancel"}},
        )
        confirmed = await change_service.confirm_preview(db_session, org.id, preview.id)
        assert confirmed.applied_summary["decisions"]["canceled"] == 1

        await db_session.refresh(slot)
        assert slot.booked_count == 1  # consumption kept
        assert (await db_session.get(Token, token.id)).status == TokenStatus.CANCELLED
