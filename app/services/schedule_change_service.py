"""
app/services/schedule_change_service.py
───────────────────────────────────────
Schedule-change impact workflow (SCH-007 / OPEN_DECISIONS Decision 11).

A schedule configuration change (weekly schedule, date exception or
capacity rule) is NEVER applied silently while bookings exist. Instead:

1. **preview**  — the proposal is validated with the exact same validation
   cores the CRUD wrappers use, and its impact is computed through the ONE
   resolution engine (`schedule_service`) with shadow-overlay parameters
   (`schedule_overlay` / `exception_overlay` / `capacity_base`). Nothing
   about generated `time_slots` or bookings is touched — the projector in
   this module is read-only.
2. **resolve**  — the administrator records an explicit decision per
   affected confirmed booking (honor / reschedule / cancel).
3. **confirm**  — after a server-side re-verification (impact digest
   re-computation + conditional status claim) the change is applied and
   the decisions executed *in one transaction*: configuration cores,
   token state transitions, in-app notifications and the audit row all
   commit together or not at all. The stale check re-runs before the
   conditional UPDATE so a stale preview can never be applied.

Decision 16 boundary: `time_slots.booked_count` / `held_count` are never
decremented here. `reschedule` decisions are recorded but blocked at
execution (no approved capacity-release policy exists); `cancel` leaves
the canceled token's place consumed, and daily quota (Decision 13A) is
never re-credited.
"""

import datetime as dt
import hashlib
import json
from typing import Any, Optional

from fastapi import HTTPException, status
from loguru import logger
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.services import audit_service, notification_service, schedule_service
from models.audit_log import AuditLog
from models.branch import Branch
from models.capacity_rule import CapacityLimitType, CapacityRule
from models.counter import Counter, CounterStatus, QueueMode
from models.notification import NotificationType
from models.organization import Organization
from models.schedule_change import (
    ScheduleChangeEntity,
    ScheduleChangePreview,
    ScheduleChangeStatus,
)
from models.schedule_exception import ScheduleException, ScheduleExceptionType
from models.time_slot import TimeSlot
from models.token import Token, TokenStatus
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule

# ── Constants ─────────────────────────────────────────────────────────────────

PREVIEW_TTL_MINUTES = 120
_DECISION_ACTIONS = ("honor", "reschedule", "cancel")
# "Affected bookings" = confirmed, still-live bookings (Decision 11).
_AFFECTED_TOKEN_STATUSES = (
    TokenStatus.WAITING,
    TokenStatus.CALLED,
    TokenStatus.ON_HOLD,
    TokenStatus.IN_PROGRESS,
)
_CANCELABLE_STATUSES = (
    TokenStatus.WAITING,
    TokenStatus.CALLED,
    TokenStatus.ON_HOLD,
)

_RESCHEDULE_BLOCKED_MESSAGE = (
    "Rescheduling would release the booking's place in its existing time slot; "
    "no approved slot-capacity release policy exists yet (OPEN_DECISIONS "
    "Decision 16), so this decision is recorded but blocked at execution."
)

# ── Error mapping ─────────────────────────────────────────────────────────────

_CONFLICT_HINTS = ("stale", "expired", "already confirmed", "already rejected",
                   "already applied", "blocked", "conflict", "missing decision")
_NOT_FOUND_HINTS = ("not found",)


def schedule_change_http_error(exc: ValueError) -> HTTPException:
    """Map service ValueError messages onto the API's status-code contract
    (404 not found, 409 workflow conflicts, 400 invalid input)."""
    message = str(exc)
    lowered = message.lower()
    if any(hint in lowered for hint in _NOT_FOUND_HINTS):
        code = status.HTTP_404_NOT_FOUND
    elif any(hint in lowered for hint in _CONFLICT_HINTS):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_400_BAD_REQUEST
    return HTTPException(status_code=code, detail=message)


# ── Impact digest ─────────────────────────────────────────────────────────────

async def _config_snapshot(db: AsyncSession, organization_id: int) -> list:
    """Tenant's schedule configuration rows as comparable JSON values."""
    rows_snapshot: list = []
    for model, label in (
        (WeeklySchedule, "weekly_schedule"),
        (ScheduleException, "schedule_exception"),
        (CapacityRule, "capacity_rule"),
    ):
        stmt = select(model.id, model.updated_at).where(
            model.organization_id == organization_id
        )
        rows_snapshot.extend(
            [label, int(row_id), updated.isoformat()]
            for row_id, updated in (await db.execute(stmt)).all()
        )
    return sorted(rows_snapshot, key=lambda r: (r[0], r[1], r[2]))


async def _affected_state_snapshot(
    db: AsyncSession,
    affected_counter_ids: list[int],
    horizon_start: dt.date,
    horizon_end_exclusive: dt.date,
) -> list:
    """Everything the digest covers for affected counters within the
    horizon: generated slots + occupancy + token states (all tokens of the
    affected counters, so status drift anywhere invalidates the digest)."""
    slots = (
        await db.execute(
            select(TimeSlot.id, TimeSlot.slot_date, TimeSlot.start_time,
                   TimeSlot.capacity, TimeSlot.booked_count, TimeSlot.held_count,
                   TimeSlot.is_active)
            .where(
                TimeSlot.counter_id.in_(affected_counter_ids),
                TimeSlot.slot_date >= horizon_start,
                TimeSlot.slot_date < horizon_end_exclusive,
            )
            .order_by(TimeSlot.id)
        )
    ).all()
    tokens = (
        await db.execute(
            select(Token.id, Token.status, Token.slot_id)
            .where(Token.counter_id.in_(affected_counter_ids))
            .order_by(Token.id)
        )
    ).all()

    snapshot: list = ["slots"]
    snapshot.extend(
        [int(s.id), s.slot_date.isoformat(), s.start_time.isoformat(),
         int(s.capacity), int(s.booked_count), int(s.held_count), bool(s.is_active)]
        for s in slots
    )
    snapshot.append("tokens")
    snapshot.extend(
        [int(t.id), str(t.status), int(t.slot_id) if t.slot_id is not None else None]
        for t in tokens
    )
    return snapshot


async def compute_impact_digest(
    db: AsyncSession,
    organization_id: int,
    affected_counter_ids: list[int],
    horizon_start: dt.date,
    horizon_days: int,
) -> str:
    """SHA-256 fingerprint over the tenant's configuration rows and the
    affected counters' slots/occupancy/token states within the horizon."""
    horizon_end = horizon_start + dt.timedelta(days=int(horizon_days))
    org = await db.get(Organization, organization_id)
    payload = [
        "org", int(organization_id),
        "org_updated", org.updated_at.isoformat() if org else None,
        "config", await _config_snapshot(db, organization_id),
        "state", await _affected_state_snapshot(
            db, affected_counter_ids, horizon_start, horizon_end
        ),
    ]
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Scope helpers ─────────────────────────────────────────────────────────────

async def _affected_counters(
    db: AsyncSession, organization_id: int, scope_level: ScheduleScopeLevel,
    scope_id: int,
) -> list[Counter]:
    """Counters whose published availability the scoped change can influence."""
    stmt = (
        select(Counter)
        .where(
            Counter.organization_id == organization_id,
            Counter.status == CounterStatus.ACTIVE,
        )
        .order_by(Counter.id)
    )
    counters = list((await db.execute(stmt)).scalars().all())
    if scope_level == ScheduleScopeLevel.COUNTER:
        counters = [c for c in counters if c.id == scope_id]
        if not counters:
            raise ValueError("Counter not found")
    elif scope_level == ScheduleScopeLevel.BRANCH:
        counters = [c for c in counters if c.branch_id == scope_id]
    elif scope_level == ScheduleScopeLevel.SERVICE:
        counters = [c for c in counters if c.service_id == scope_id]
    # ORGANIZATION scope affects every active counter.
    return counters


# ── Read-only slot projection ─────────────────────────────────────────────────

async def _project_slots(
    db: AsyncSession,
    organization: Organization,
    branch: Optional[Branch],
    counter: Counter,
    target_date: dt.date,
    schedule_overlay: Optional[schedule_service.ROScheduleOverlay],
    exception_overlay: Optional[schedule_service.ROExceptionOverlay],
    capacity_base: Optional[int],
) -> dict:
    """Compute the WOULD-BE slot grid for one date without any write — a
    faithful read-only mirror of `schedule_service.generate_slots_for_date`
    (same resolution data, same interval/UTC/capacity rules) parameterized
    by the preview overlays."""
    availability = await schedule_service.resolve_day_availability(
        db, organization, target_date, branch=branch, counter=counter,
        schedule_overlay=schedule_overlay, exception_overlay=exception_overlay,
    )
    projected = {
        "date": target_date.isoformat(),
        "state": availability.state.value,
        "reason": availability.reason,
        "slot_capacity": None,
        "slot_times": [],
        "timezone_conflict": None,
    }
    if (
        counter.queue_mode != QueueMode.SLOTS
        or availability.state not in (
            schedule_service.AvailabilityState.AVAILABLE,
            schedule_service.AvailabilityState.LIMITED,
            schedule_service.AvailabilityState.FULL,
        )
    ):
        return projected
    if availability.opens_at is None or availability.closes_at is None:
        return projected

    interval = availability.slot_interval_minutes
    if not interval or interval <= 0:
        return projected

    capacity = await schedule_service.get_slot_capacity(
        db, counter.organization_id, counter,
        capacity_factor=availability.capacity_factor,
        capacity_base=capacity_base,
    )
    projected["slot_capacity"] = capacity

    tz_name = availability.timezone_name
    cursor = dt.datetime.combine(target_date, availability.opens_at)
    close_dt = dt.datetime.combine(target_date, availability.closes_at)
    times: list[dict] = []
    while True:
        end = cursor + dt.timedelta(minutes=interval)
        if end > close_dt:
            break
        try:
            start_utc = schedule_service.utc_slot_time(target_date, cursor.time(), tz_name)
            end_utc = schedule_service.utc_slot_time(target_date, end.time(), tz_name)
        except Exception:  # unknown/overspecified tz: recorded, not fatal (SCH-009 owns tz health)
            projected["timezone_conflict"] = f"Unresolvable timezone {tz_name!r} on {target_date}."
            break
        if start_utc is None or end_utc is None:
            projected["timezone_conflict"] = (
                f"Operating hours on {target_date} in timezone {tz_name!r} cross a UTC "
                f"date boundary around {cursor.time():%H:%M}-{end.time():%H:%M} local."
            )
            break
        times.append({
            "start_local": cursor.time().isoformat(),
            "start_utc": start_utc.isoformat(),
            "end_utc": end_utc.isoformat(),
        })
        cursor = end
    projected["slot_times"] = times
    return projected


# ── Proposal normalisation ───────────────────────────────────────────────────

async def _normalized_proposal(
    db: AsyncSession,
    organization_id: int,
    entity: ScheduleChangeEntity,
    edit_type: str,
    target_row: Optional[Any],
    data: dict,
) -> tuple[dict, ScheduleScopeLevel, int]:
    """Validate the proposal with the SAME validation cores CRUD uses and
    return (canonical proposal fields, scope level, scope id). Commit-free."""
    if entity == ScheduleChangeEntity.WEEKLY_SCHEDULE:
        if edit_type == "create":
            vals = await schedule_service._new_weekly_values(db, organization_id, data)
        elif edit_type == "update":
            vals = await schedule_service._prospective_weekly_values(
                db, organization_id, target_row, data
            )
        else:  # delete
            vals = {
                "id": target_row.id,
                "scope_level": target_row.scope_level,
                "scope_id": target_row.scope_id,
                "day_of_week": target_row.day_of_week,
                "is_closed": target_row.is_closed,
                "open_time": target_row.open_time,
                "close_time": target_row.close_time,
                "slot_interval_minutes": target_row.slot_interval_minutes,
            }
            vals = {k: (v.value if hasattr(v, "value") else v) for k, v in vals.items()}
            vals["open_time"] = str(vals["open_time"]) if vals["open_time"] else None
            vals["close_time"] = str(vals["close_time"]) if vals["close_time"] else None
        scope_level = vals["scope_level"]
        scope_level = (
            scope_level
            if isinstance(scope_level, ScheduleScopeLevel)
            else ScheduleScopeLevel(scope_level)
        )
        scope_id = int(vals["scope_id"])
        for key in ("open_time", "close_time"):
            if vals.get(key) is not None and not isinstance(vals[key], str):
                vals[key] = vals[key].isoformat()
        proposal = {
            "day_of_week": int(vals["day_of_week"]),
            "is_closed": bool(vals["is_closed"]),
            "open_time": vals["open_time"],
            "close_time": vals["close_time"],
            "slot_interval_minutes": vals["slot_interval_minutes"],
        }
        return proposal, scope_level, scope_id

    if entity == ScheduleChangeEntity.SCHEDULE_EXCEPTION:
        if edit_type == "create":
            vals = await schedule_service._new_exception_values(db, organization_id, data)
        elif edit_type == "update":
            vals = await schedule_service._prospective_exception_values(
                db, organization_id, target_row, data
            )
        else:
            vals = {
                "scope_level": target_row.scope_level,
                "scope_id": target_row.scope_id,
                "exception_date": target_row.exception_date,
                "exception_type": target_row.exception_type,
                "reason": target_row.reason,
                "opens_at": target_row.opens_at,
                "closes_at": target_row.closes_at,
                "capacity_factor": target_row.capacity_factor,
            }
        scope_level = vals["scope_level"]
        scope_level = (
            scope_level
            if isinstance(scope_level, ScheduleScopeLevel)
            else ScheduleScopeLevel(scope_level)
        )
        scope_id = int(vals["scope_id"])
        proposal = {
            "exception_date": vals["exception_date"].isoformat()
            if not isinstance(vals["exception_date"], str) else vals["exception_date"],
            "exception_type": vals["exception_type"].value
            if not isinstance(vals["exception_type"], str) else vals["exception_type"],
            "reason": vals["reason"],
            "opens_at": vals["opens_at"].isoformat()
            if vals["opens_at"] is not None and not isinstance(vals["opens_at"], str)
            else vals["opens_at"],
            "closes_at": vals["closes_at"].isoformat()
            if vals["closes_at"] is not None and not isinstance(vals["closes_at"], str)
            else vals["closes_at"],
            "capacity_factor": (
                str(vals["capacity_factor"]) if vals["capacity_factor"] is not None else None
            ),
        }
        return proposal, scope_level, scope_id

    # CAPACITY_RULE
    if edit_type == "create":
        vals = await schedule_service._new_capacity_values(db, organization_id, data)
    elif edit_type == "update":
        vals = await schedule_service._prospective_capacity_values(
            db, organization_id, target_row, data
        )
    else:
        vals = {
            "scope_level": target_row.scope_level,
            "scope_id": target_row.scope_id,
            "limit_type": target_row.limit_type,
            "booking_source": target_row.booking_source,
            "max_count": target_row.max_count,
        }
    scope_level = vals["scope_level"]
    scope_level = (
        scope_level
        if isinstance(scope_level, ScheduleScopeLevel)
        else ScheduleScopeLevel(scope_level)
    )
    scope_id = int(vals["scope_id"])
    limit_type = vals["limit_type"]
    limit_type = (
        limit_type
        if isinstance(limit_type, CapacityLimitType)
        else CapacityLimitType(limit_type)
    )
    proposal = {
        "limit_type": limit_type.value,
        "booking_source": str(vals["booking_source"]),
        "max_count": int(vals["max_count"]),
        "is_active": bool(vals.get("is_active", True)),
    }
    return proposal, scope_level, scope_id


# ── Overlay construction ─────────────────────────────────────────────────────

def _time_of(value: Any) -> Optional[dt.time]:
    if value is None:
        return None
    if isinstance(value, dt.time):
        return value
    return dt.time.fromisoformat(value)


def _date_of(value: Any) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(value)


def _weekly_shadow_row(proposal: dict) -> schedule_service.ShadowScheduleRow:
    return schedule_service.ShadowScheduleRow(
        is_closed=bool(proposal["is_closed"]),
        open_time=_time_of(proposal["open_time"]),
        close_time=_time_of(proposal["close_time"]),
        slot_interval_minutes=proposal["slot_interval_minutes"],
    )


def _exception_shadow_row(proposal: dict) -> schedule_service.ShadowExceptionRow:
    factor = proposal.get("capacity_factor")
    return schedule_service.ShadowExceptionRow(
        exception_type=ScheduleExceptionType(proposal["exception_type"]),
        reason=proposal.get("reason") or "",
        opens_at=_time_of(proposal.get("opens_at")),
        closes_at=_time_of(proposal.get("closes_at")),
        capacity_factor=None if factor is None else schedule_service.Decimal(str(factor)),
        is_active=True,
    )


def _build_overlays(
    entity: ScheduleChangeEntity,
    edit_type: str,
    target_row: Optional[Any],
    proposal: dict,
    scope_level: ScheduleScopeLevel,
    scope_id: int,
) -> tuple[
    schedule_service.ROScheduleOverlay,
    schedule_service.ROExceptionOverlay,
    Optional[int],
]:
    """Shadow/override inputs for the resolution engine, matching the
    post-application state of the proposal."""
    schedule_overlay: schedule_service.ROScheduleOverlay = {}
    exception_overlay: schedule_service.ROExceptionOverlay = {}
    capacity_base: Optional[int] = None

    if entity == ScheduleChangeEntity.WEEKLY_SCHEDULE:
        dow = int(proposal["day_of_week"])
        old_dow = int(target_row.day_of_week) if target_row is not None else None
        key = (scope_level.value, scope_id, dow)
        if edit_type == "create":
            schedule_overlay[key] = _weekly_shadow_row(proposal)
        else:
            # Any old-key row is superseded: removed for update/delete.
            schedule_overlay[(scope_level.value, scope_id, old_dow)] = None
            if edit_type == "update":
                schedule_overlay[key] = _weekly_shadow_row(proposal)
    elif entity == ScheduleChangeEntity.SCHEDULE_EXCEPTION:
        target_date = _date_of(proposal["exception_date"])
        old_date = (
            _date_of(target_row.exception_date) if target_row is not None else None
        )
        if edit_type == "create":
            exception_overlay[(scope_level.value, scope_id, target_date)] = (
                _exception_shadow_row(proposal)
            )
        else:
            exception_overlay[(scope_level.value, scope_id, old_date)] = None
            if edit_type == "update":
                exception_overlay[(scope_level.value, scope_id, target_date)] = (
                    _exception_shadow_row(proposal)
                )
    else:  # CAPACITY_RULE
        if proposal["limit_type"] == CapacityLimitType.SLOT_CAPACITY.value:
            # Only counter-scoped slot_capacity rules influence per-slot
            # capacity in the resolution engine (`get_slot_capacity` reads
            # counter-scope rules); other scopes have no slot-side impact.
            if edit_type == "delete" and scope_level == ScheduleScopeLevel.COUNTER:
                capacity_base = schedule_service.DEFAULT_SLOT_CAPACITY
            elif (
                edit_type in ("create", "update")
                and proposal.get("is_active", True)
                and scope_level == ScheduleScopeLevel.COUNTER
            ):
                capacity_base = int(proposal["max_count"])

    return schedule_overlay, exception_overlay, capacity_base


# ── Impact computation ───────────────────────────────────────────────────────

def _serialize_target(target_row: Any, entity: ScheduleChangeEntity) -> dict:
    if target_row is None:
        return {}
    return {
        "id": target_row.id,
        "scope_level": target_row.scope_level.value
        if hasattr(target_row.scope_level, "value") else str(target_row.scope_level),
        "scope_id": target_row.scope_id,
    }


async def compute_impact(
    db: AsyncSession,
    organization: Organization,
    entity: ScheduleChangeEntity,
    edit_type: str,
    target_row: Optional[Any],
    proposal: dict,
    scope_level: ScheduleScopeLevel,
    scope_id: int,
    schedule_overlay: schedule_service.ROScheduleOverlay,
    exception_overlay: schedule_service.ROExceptionOverlay,
    capacity_base: Optional[int],
) -> dict:
    """Compute the impact report over the booking horizon using the ONE
    resolution engine with the preview overlays (read-only)."""
    counters = await _affected_counters(db, organization.id, scope_level, scope_id)
    horizon_start = schedule_service.get_tenant_today(organization, None)
    horizon_days = schedule_service.MAX_ADVANCE_BOOKING_DAYS + 1
    horizon_dates = [
        horizon_start + dt.timedelta(days=offset) for offset in range(horizon_days)
    ]

    counter_reports: list[dict] = []
    affected_bookings: list[dict] = []
    occupancy_conflicts: list[dict] = []

    for counter in counters:
        branch = (
            await db.get(Branch, counter.branch_id)
            if counter.branch_id is not None else None
        )
        day_reports: list[dict] = []
        counter_conflicts: list[dict] = []
        for target_date in horizon_dates:
            projected = await _project_slots(
                db, organization, branch, counter, target_date,
                schedule_overlay, exception_overlay, capacity_base,
            )
            existing = await schedule_service.list_slots_for_date(
                db, counter.id, target_date
            )
            existing_times = sorted(
                s.start_time.isoformat() for s in existing
                if s.is_active
            )
            proposed_times = sorted(t["start_utc"] for t in projected["slot_times"])
            would_be_capacity = projected["slot_capacity"]

            # Confirmed-booking occupancy vs the proposed capacity.
            if would_be_capacity is not None:
                for slot in existing:
                    occupancy = int(slot.booked_count) + int(slot.held_count)
                    if occupancy > would_be_capacity:
                        conflict = {
                            "counter_id": counter.id,
                            "slot_id": slot.id,
                            "slot_date": slot.slot_date.isoformat(),
                            "start_utc": slot.start_time.isoformat(),
                            "occupancy": occupancy,
                            "proposed_capacity": would_be_capacity,
                        }
                        counter_conflicts.append(conflict)
                        occupancy_conflicts.append(conflict)

            changed = proposed_times != existing_times
            if projected["timezone_conflict"] is not None:
                counter_conflicts.append({
                    "counter_id": counter.id,
                    "type": "timezone",
                    "detail": projected["timezone_conflict"],
                })
            day_reports.append({
                "date": target_date.isoformat(),
                "state": projected["state"],
                "reason": projected["reason"],
                "slot_capacity": would_be_capacity,
                "existing_slot_times": existing_times,
                "proposed_slot_times": proposed_times,
                "changed": changed,
                "timezone_conflict": projected["timezone_conflict"],
            })

            # Affected bookings: live tokens in this counter's slots for the day.
            slot_ids = [s.id for s in existing]
            if slot_ids:
                tokens = (
                    await db.execute(
                        select(Token).where(
                            Token.slot_id.in_(slot_ids),
                            Token.status.in_(_AFFECTED_TOKEN_STATUSES),
                        )
                    )
                ).scalars().all()
                for token in tokens:
                    affected_bookings.append({
                        "token_id": token.id,
                        "token_number": token.token_number,
                        "counter_id": counter.id,
                        "slot_id": token.slot_id,
                        "slot_date": target_date.isoformat(),
                        "user_id": token.user_id,
                        "status": str(token.status.value)
                        if hasattr(token.status, "value") else str(token.status),
                    })

        counter_reports.append({
            "counter_id": counter.id,
            "counter_name": counter.counter_name,
            "branch_id": counter.branch_id,
            "days": day_reports,
        })

    return {
        "entity": entity.value,
        "edit_type": edit_type,
        "proposal": proposal,
        "target": _serialize_target(target_row, entity),
        "scope": {"level": scope_level.value, "id": scope_id},
        "horizon": {
            "start": horizon_start.isoformat(),
            "days": horizon_days,
        },
        "counters": counter_reports,
        "affected_bookings": affected_bookings,
        "occupancy_conflicts": occupancy_conflicts,
        "summary": {
            "counters_affected": len(counter_reports),
            "bookings_affected": len(affected_bookings),
            "occupancy_conflicts": len(occupancy_conflicts),
        },
    }


def tokens_status_values() -> list[str]:
    """Statuses of live bookings for the affected-booking query (kept for
    readability/debugging; the engine compares against the enum directly)."""
    return [s.value for s in _AFFECTED_TOKEN_STATUSES]


# ── Preview CRUD ─────────────────────────────────────────────────────────────

def _get_target_row(
    db: AsyncSession, organization_id: int, entity: ScheduleChangeEntity, target_id: int
):
    if entity == ScheduleChangeEntity.WEEKLY_SCHEDULE:
        return schedule_service.get_weekly_schedule(db, organization_id, target_id)
    if entity == ScheduleChangeEntity.SCHEDULE_EXCEPTION:
        return schedule_service.get_schedule_exception(db, organization_id, target_id)
    return schedule_service.get_capacity_rule(db, organization_id, target_id)


def _entity_model(entity: ScheduleChangeEntity):
    return {
        ScheduleChangeEntity.WEEKLY_SCHEDULE: WeeklySchedule,
        ScheduleChangeEntity.SCHEDULE_EXCEPTION: ScheduleException,
        ScheduleChangeEntity.CAPACITY_RULE: CapacityRule,
    }[entity]


async def create_preview(
    db: AsyncSession,
    organization: Organization,
    data: dict,
    created_by: Optional[str] = None,
) -> ScheduleChangePreview:
    """Validate the proposal, compute its impact through the resolution
    engine's overlays, fingerprint the affected state and persist one
    PENDING preview row. Commit-free cores → no side effects on slots."""
    try:
        entity = ScheduleChangeEntity(data["entity"])
    except (KeyError, ValueError) as exc:
        raise ValueError("Unknown schedule-change entity") from exc
    edit_type = data.get("edit_type")
    if edit_type not in ("create", "update", "delete"):
        raise ValueError("edit_type must be one of: create, update, delete")

    target_row = None
    target_id = data.get("target_id")
    if edit_type in ("update", "delete"):
        if target_id is None:
            raise ValueError("target_id is required for update and delete previews")
        target_row = await _get_target_row(db, organization.id, entity, int(target_id))

    # `data.data` carries the same payload the corresponding CRUD endpoint
    # would accept; the outer keys (entity/edit_type/target_id) are workflow
    # metadata, not configuration fields.
    proposal_data = data.get("data") or {}
    proposal, scope_level, scope_id = await _normalized_proposal(
        db, organization.id, entity, edit_type, target_row, proposal_data
    )
    schedule_overlay, exception_overlay, capacity_base = _build_overlays(
        entity, edit_type, target_row, proposal, scope_level, scope_id
    )
    impact = await compute_impact(
        db, organization, entity, edit_type, target_row, proposal,
        scope_level, scope_id,
        schedule_overlay, exception_overlay, capacity_base,
    )

    horizon_start = _date_of(impact["horizon"]["start"])
    horizon_days = int(impact["horizon"]["days"])
    affected_counter_ids = list(
        {report["counter_id"] for report in impact["counters"]}
    )
    digest = await compute_impact_digest(
        db, organization.id, affected_counter_ids, horizon_start, horizon_days
    )

    # Cross-check: state changed between impact computation and fingerprint →
    # report is already stale; refuse to publish a preview that cannot be
    # confirmed against the state it saw.
    digest_after = await compute_impact_digest(
        db, organization.id, affected_counter_ids, horizon_start, horizon_days
    )
    if digest_after != digest:
        raise ValueError("conflict: schedule state changed during preview computation; retry")

    now = dt.datetime.now(dt.timezone.utc)
    row = ScheduleChangePreview(
        organization_id=organization.id,
        entity=entity,
        edit_type=edit_type,
        target_id=None if edit_type == "create" else (target_id and int(target_id)),
        status=ScheduleChangeStatus.PENDING,
        proposal={
            "entity": entity.value,
            "edit_type": edit_type,
            "data": proposal_data,
            "fields": proposal,
        },
        impact=impact,
        decisions={},
        impact_digest=digest,
        affected_counter_ids=affected_counter_ids,
        horizon_start=horizon_start,
        horizon_days=horizon_days,
        expires_at=now + dt.timedelta(minutes=PREVIEW_TTL_MINUTES),
        created_by=created_by or "organization",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "SCH-007 preview {} created for org {} ({}, {}, affected bookings: {})",
        row.id, organization.id, entity.value, edit_type, impact["summary"]["bookings_affected"],
    )
    return row


async def list_previews(
    db: AsyncSession,
    organization_id: int,
    status_filter: Optional[str] = None,
    limit: int = 50,
) -> list[ScheduleChangePreview]:
    stmt = select(ScheduleChangePreview).where(
        ScheduleChangePreview.organization_id == organization_id
    )
    if status_filter:
        stmt = stmt.where(
            ScheduleChangePreview.status == ScheduleChangeStatus(status_filter)
        )
    stmt = stmt.order_by(ScheduleChangePreview.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def get_preview(
    db: AsyncSession, organization_id: int, preview_id: int
) -> ScheduleChangePreview:
    row = await db.get(ScheduleChangePreview, preview_id)
    if row is None or row.organization_id != organization_id:
        raise ValueError("Schedule change preview not found")
    return row


def _affected_booking_map(impact: dict) -> dict[str, dict]:
    return {
        str(booking["token_id"]): booking for booking in impact["affected_bookings"]
    }


async def _require_active_pending(
    db: AsyncSession, row: ScheduleChangePreview
) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    if row.status != ScheduleChangeStatus.PENDING:
        raise ValueError(f"already {row.status.value}: decision steps are final")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
    if expires_at <= now:
        row.status = ScheduleChangeStatus.EXPIRED
        await db.commit()
        raise ValueError("expired: create a new schedule change preview")


# ── Resolve step ─────────────────────────────────────────────────────────────

async def record_decisions(
    db: AsyncSession,
    organization_id: int,
    preview_id: int,
    decisions: dict[str, dict],
    resolved_by: Optional[str] = None,
) -> ScheduleChangePreview:
    """Record the administrator's per-booking resolution decisions. Decisions
    are only PERSISTED here; they execute at confirm (Decision 11: explicit,
    audited, non-magical)."""
    row = await get_preview(db, organization_id, preview_id)
    await _require_active_pending(db, row)

    booking_map = _affected_booking_map(row.impact)
    current = dict(row.decisions or {})
    for token_id_str, decision in decisions.items():
        if token_id_str not in booking_map:
            raise ValueError(
                f"Token {token_id_str} is not an affected booking of this preview"
            )
        action = decision.get("decision")
        if action not in _DECISION_ACTIONS:
            raise ValueError(
                f"Unknown decision {action!r}: expected one of {list(_DECISION_ACTIONS)}"
            )
        entry = {
            "decision": action,
            "reason": decision.get("reason"),
            "resolved_by": resolved_by or "organization",
            "resolved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        stored = current.get(token_id_str, {})
        if stored.get("outcome") is not None:
            entry["outcome"] = stored["outcome"]
            entry["outcome_detail"] = stored.get("outcome_detail")
            entry["resolved_at"] = stored.get("resolved_at")
            entry["resolved_by"] = stored.get("resolved_by")
        current[token_id_str] = entry

    db.add(
        AuditLog(
            actor_type="organization",
            actor_id=None,
            actor_email=resolved_by,
            organization_id=organization_id,
            action="schedule.change.resolve",
            target_type="schedule_change_preview",
            target_id=str(row.id),
            outcome="success",
            details=audit_service._sanitize_details({
                "decisions": {k: v.get("decision") for k, v in current.items()},
            }),
            created_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    row.decisions = current
    await db.commit()
    await db.refresh(row)
    return row


# ── Confirm step ─────────────────────────────────────────────────────────────

def _coerce_proposal_data(entity: ScheduleChangeEntity, data: dict) -> dict:
    """Reconstruct typed values for the CRUD cores at confirm time. The
    stored proposal carries plain JSON types (HH:MM:SS strings, ISO dates);
    the cores validated them at preview time, and the DB columns need
    `datetime.time` / `datetime.date` / `Decimal` instances."""
    data = dict(data)
    if entity == ScheduleChangeEntity.WEEKLY_SCHEDULE:
        data["is_closed"] = bool(data.get("is_closed", False))
        data["open_time"] = _time_of(data.get("open_time"))
        data["close_time"] = _time_of(data.get("close_time"))
        if data.get("slot_interval_minutes") is not None:
            data["slot_interval_minutes"] = int(data["slot_interval_minutes"])
        if data.get("day_of_week") is not None:
            data["day_of_week"] = int(data["day_of_week"])
    elif entity == ScheduleChangeEntity.SCHEDULE_EXCEPTION:
        if data.get("exception_date") is not None:
            data["exception_date"] = _date_of(data["exception_date"])
        data["opens_at"] = _time_of(data.get("opens_at"))
        data["closes_at"] = _time_of(data.get("closes_at"))
        if data.get("capacity_factor") is not None:
            data["capacity_factor"] = schedule_service.Decimal(str(data["capacity_factor"]))
        if data.get("is_active") is not None:
            data["is_active"] = bool(data["is_active"])
    else:
        data["max_count"] = int(data["max_count"])
        if data.get("is_active") is not None:
            data["is_active"] = bool(data["is_active"])
    return data


async def _apply_config_change(
    db: AsyncSession,
    organization_id: int,
    entity: ScheduleChangeEntity,
    edit_type: str,
    target_id: Optional[int],
    data: dict,
) -> dict:
    """Apply the configuration change via the commit-free CRUD cores
    (single transaction — no intermediate commits)."""
    data = _coerce_proposal_data(entity, data)
    if entity == ScheduleChangeEntity.WEEKLY_SCHEDULE:
        if edit_type == "create":
            row = await schedule_service._create_weekly_core(db, organization_id, data)
            return {"created": row.id}
        if edit_type == "update":
            row = await schedule_service._update_weekly_core(
                db, organization_id, int(target_id), data
            )
            return {"updated": row.id}
        await schedule_service._delete_weekly_core(db, organization_id, int(target_id))
        return {"deleted": int(target_id)}
    if entity == ScheduleChangeEntity.SCHEDULE_EXCEPTION:
        if edit_type == "create":
            row = await schedule_service._create_exception_core(db, organization_id, data)
            return {"created": row.id}
        if edit_type == "update":
            row = await schedule_service._update_exception_core(
                db, organization_id, int(target_id), data
            )
            return {"updated": row.id}
        await schedule_service._delete_exception_core(db, organization_id, int(target_id))
        return {"deleted": int(target_id)}
    if edit_type == "create":
        row = await schedule_service._create_capacity_core(db, organization_id, data)
        return {"created": row.id}
    if edit_type == "update":
        row = await schedule_service._update_capacity_core(
            db, organization_id, int(target_id), data
        )
        return {"updated": row.id}
    await schedule_service._delete_capacity_core(db, organization_id, int(target_id))
    return {"deleted": int(target_id)}


def _token_status(token: Token) -> str:
    return token.status.value if hasattr(token.status, "value") else str(token.status)


async def _execute_decisions(
    db: AsyncSession,
    org_id: int,
    row: ScheduleChangePreview,
    resolved_by: Optional[str],
) -> dict:
    """Execute recorded decisions atomically inside the confirm transaction.

    Decision 16 boundary: confirmation NEVER releases slot capacity.
    - honor: no state change.
    - reschedule: blocked outcome (recorded, not executed).
    - cancel: token state changes only; `booked_count` stays consumed and
      Decision 13A daily-quota accounting is untouched.
    Returns the summary for `applied_summary`."""
    row.decisions = dict(row.decisions or {})
    from app.services import token_service  # local import (single-commit reuse)

    executed = {"canceled": 0, "honored": 0, "reschedules_blocked": 0, "notified": 0}
    actor_role = "organization"

    for token_id_str, stored in row.decisions.items():
        decision = stored.get("decision")
        if decision is None:
            raise ValueError(
                f"missing decision for affected booking {token_id_str}"
            )
        if decision == "honor":
            stored["outcome"] = "honor_confirmed"
            stored["outcome_detail"] = "Booking honored: no changes applied."
            executed["honored"] += 1
        elif decision == "reschedule":
            stored["outcome"] = "reschedule_blocked"
            stored["outcome_detail"] = _RESCHEDULE_BLOCKED_MESSAGE
            executed["reschedules_blocked"] += 1
        else:  # cancel
            token = await db.get(Token, int(token_id_str))
            if token is None or token.organization_id != org_id:
                stored["outcome"] = "cancel_blocked"
                stored["outcome_detail"] = "Token not found in the organization."
                continue
            if token.status not in _CANCELABLE_STATUSES:
                stored["outcome"] = "cancel_blocked"
                stored["outcome_detail"] = (
                    f"Token status is {_token_status(token)}; it is no longer "
                    "cancelable through a schedule change."
                )
                continue
            old_status = token.status
            token.status = TokenStatus.CANCELLED
            await token_service._recalculate_positions(db, token.counter_id)
            await token_service._record_history(
                db, token, old_status, TokenStatus.CANCELLED.value
                if hasattr(TokenStatus.CANCELLED, "value") else str(TokenStatus.CANCELLED),
                actor_role, None,
            )
            await token_service._log_action(
                db, org_id, token.counter_id, token.id,
                "token_cancelled", actor_role, None,
                meta={"via": "schedule_change_confirm", "preview_id": row.id},
            )
            stored["outcome"] = "cancelled"
            stored["outcome_detail"] = "Booking cancelled; its slot place remains consumed."
            executed["canceled"] += 1

            notif_user_id = token.user_id
            # guest token: no notification (no user account to notify)
            if notif_user_id is not None:
                await notification_service.create_in_app(
                    db, notif_user_id, NotificationType.SCHEDULE_CHANGE,
                    subject="Your booking was affected by a schedule change",
                    message=(
                        f"Your booking {token.token_number} was cancelled following "
                        "a confirmed schedule change made by the organization."
                    ),
                    token_id=token.id,
                )
                executed["notified"] += 1
    # Mid-transaction flushes (position recalculation, notification INSERT)
    # already serialized row.decisions; mark the mutable JSON column dirty so
    # the final UPDATE persists the outcome entries too.
    row.decisions = dict(row.decisions)
    flag_modified(row, "decisions")
    return executed


async def confirm_preview(
    db: AsyncSession,
    organization_id: int,
    preview_id: int,
    confirmed_by: Optional[str] = None,
) -> ScheduleChangePreview:
    """Dry-run-free, single-transaction confirmation:
    digest recheck → missing-decision guard → conditional status claim →
    apply config via CRUD cores → execute decisions → notify → audit →
    commit (rollback on any failure)."""
    row = await get_preview(db, organization_id, preview_id)
    await _require_active_pending(db, row)

    # Missing decisions block confirmation (Decision 11: explicit, per booking).
    booking_map = _affected_booking_map(row.impact)
    missing = sorted(
        tid for tid in booking_map
        if (row.decisions or {}).get(tid, {}).get("decision") is None
    )
    if missing:
        raise ValueError(
            f"blocked: missing resolution decisions for affected bookings {missing}"
        )

    fresh_digest = await compute_impact_digest(
        db, organization_id, row.affected_counter_ids,
        row.horizon_start, row.horizon_days,
    )
    if fresh_digest != row.impact_digest:
        now = dt.datetime.now(dt.timezone.utc)
        await db.execute(
            update(ScheduleChangePreview)
            .where(
                ScheduleChangePreview.id == row.id,
                ScheduleChangePreview.status == ScheduleChangeStatus.PENDING,
            )
            .values(status=ScheduleChangeStatus.STALE, updated_at=now)
        )
        await db.commit()
        await db.refresh(row)
        raise ValueError("stale: schedule state changed since this preview was created")

    # Conditional claim: only one transaction can move pending → confirmed
    # (SQLite serializes writes; MySQL's conditional UPDATE row-locks).
    now = dt.datetime.now(dt.timezone.utc)
    result = await db.execute(
        update(ScheduleChangePreview)
        .where(
            ScheduleChangePreview.id == row.id,
            ScheduleChangePreview.status == ScheduleChangeStatus.PENDING,
        )
        .values(status=ScheduleChangeStatus.CONFIRMED, confirmed_at=now)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise ValueError("conflict: preview was already resolved by a concurrent request")
    # Mirror the claimed state onto the identity-mapped instance (the
    # conditional UPDATE bypasses the ORM identity cache).
    row.status = ScheduleChangeStatus.CONFIRMED
    row.confirmed_at = now

    try:
        applied_config = await _apply_config_change(
            db, organization_id, row.entity, row.edit_type,
            row.target_id, row.proposal["data"],
        )
        decision_execution = await _execute_decisions(
            db, organization_id, row, confirmed_by
        )
    except Exception:
        await db.rollback()
        logger.warning("SCH-007 confirm of preview {} failed and rolled back", row.id)
        raise

    row.applied_summary = {
        "config": applied_config,
        "decisions": decision_execution,
        "confirmed_by": confirmed_by or "organization",
        "confirmed_at": now.isoformat(),
    }
    # In-transaction audit (audit_service.log_audit_event commits internally,
    # breaking single-transaction semantics — insert directly instead).
    db.add(AuditLog(
        actor_type="organization",
        actor_id=None,
        actor_email=confirmed_by,
        organization_id=organization_id,
        action="schedule.change.confirm",
        target_type="schedule_change_preview",
        target_id=str(row.id),
        outcome="success",
        details=audit_service._sanitize_details({
            "entity": row.entity.value if hasattr(row.entity, "value") else str(row.entity),
            "edit_type": row.edit_type,
            "config": applied_config,
            "decisions": decision_execution,
            "impact_digest": row.impact_digest,
        }),
        ip_address=None,
        created_at=now,
    ))
    await db.commit()
    await db.refresh(row)
    logger.info("SCH-007 preview {} confirmed for org {}", row.id, organization_id)
    return row


async def reject_preview(
    db: AsyncSession,
    organization_id: int,
    preview_id: int,
    rejected_by: Optional[str] = None,
) -> ScheduleChangePreview:
    """Explicitly abandon a pending preview (no configuration is modified)."""
    row = await get_preview(db, organization_id, preview_id)
    now = dt.datetime.now(dt.timezone.utc)
    if row.status != ScheduleChangeStatus.PENDING:
        raise ValueError(f"already {row.status.value}")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
    if expires_at <= now:
        row.status = ScheduleChangeStatus.EXPIRED
        row.applied_summary = {"rejected_by": None, "expired_before_reject": True}
        await db.commit()
        await db.refresh(row)
        raise ValueError("expired: create a new schedule change preview")

    result = await db.execute(
        update(ScheduleChangePreview)
        .where(
            ScheduleChangePreview.id == row.id,
            ScheduleChangePreview.status == ScheduleChangeStatus.PENDING,
        )
        .values(status=ScheduleChangeStatus.REJECTED, confirmed_at=None, updated_at=now)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise ValueError("conflict: preview was already resolved by a concurrent request")
    row.applied_summary = {
        "rejected_by": rejected_by or "organization",
        "rejected_at": now.isoformat(),
    }
    db.add(AuditLog(
        actor_type="organization",
        actor_id=None,
        actor_email=rejected_by,
        organization_id=organization_id,
        action="schedule.change.reject",
        target_type="schedule_change_preview",
        target_id=str(row.id),
        outcome="success",
        details=audit_service._sanitize_details({
            "entity": row.entity.value if hasattr(row.entity, "value") else str(row.entity),
            "edit_type": row.edit_type,
        }),
        ip_address=None,
        created_at=now,
    ))
    await db.commit()
    await db.refresh(row)
    return row
