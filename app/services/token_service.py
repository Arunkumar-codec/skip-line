"""
app/services/token_service.py
──────────────────────────────
Complete token lifecycle: book → call → complete / skip / cancel.
Also handles queue position calculation and real-time broadcast triggers.

Slot-mode booking (Phase 3 Task 3): counters with `queue_mode = slots`
require an explicit `slot_id`; the reservation is a guarded atomic
`UPDATE … SET booked_count = booked_count + 1 WHERE … AND
booked_count + held_count < capacity` executed in the SAME transaction
that creates the token (single commit — see OPEN_DECISIONS §4).
Day-queue counters book exactly as in Phases 1-2 (`slot_id = NULL`).
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from loguru import logger
from sqlalchemy import func, select, and_, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.branch import Branch
from models.capacity_rule import BookingSource, CapacityLimitType, CapacityRule
from models.counter import Counter, CounterStatus, QueueMode
from models.organization import Organization
from models.queue_log import QueueLog
from models.service import Service
from models.sub_service import SubService
from models.time_slot import TimeSlot
from models.token import Token, TokenStatus
from models.token_history import TokenHistory
from models.token_service_item import TokenServiceItem
from models.weekly_schedule import ScheduleScopeLevel
from app.services.schedule_service import (
    AvailabilityState,
    MAX_ADVANCE_BOOKING_DAYS,
    get_tenant_today,
)
from app.utils import qr_generator


# Booking must be rejected when the resolved day state cannot accept
# appointments (SCH-003/SCH-008 — closures and unavailability win).
_SLOT_BLOCKING_STATES = frozenset({
    AvailabilityState.CLOSED,
    AvailabilityState.HOLIDAY,
    AvailabilityState.NOT_OPEN,
    AvailabilityState.SERVICE_UNAVAILABLE,
})

# Layered capacity-rule checks (SCH-004): scopes of a CapacityRule that
# can gate a booking, in `(scope_level value, scope_id, rule)` triples.
_SCOPE_LABELS = {
    "org": "organization",
    "branch": "branch",
    "counter": "counter",
    "service": "service",
}


def normalize_booking_source(value) -> str:
    """Coerce a server-provided issuing channel to its stored value.

    Routers inject the constant for their channel (kiosk/online); direct
    service callers default to `walk_in` (day-queue issuance, Phase 1-2
    behavior). A client never chooses this — CapacityRule counting keys
    on the stored values of `models.capacity_rule.BookingSource`.
    """
    if value is None:
        return BookingSource.WALK_IN.value
    try:
        return BookingSource(value).value
    except ValueError:
        raise ValueError(f"Invalid booking source: {value!r}")


# ── SCH-004 capacity-rule engine ──────────────────────────────────────────────
#
# Layers applied at booking (OPEN_DECISIONS Decision 13 reconciliation:
# "Daily issuance limits (Decision 13), per-slot capacity, counter daily
# limits, service-level limits, and source-based allocation (SCH-004;
# CTR-003) are enforced as independent, layered checks"). Each applicable
# rule must pass — no precedence overrides, rejects happen before the
# slot reservation so a rejected booking consumes no occupancy.
#
# Counting semantics (documented in OPEN_DECISIONS §4):
# - bucket = ISSUANCE day, the UTC calendar day of `created_at` — identical
#   to the pre-existing `counter.daily_limit` bucket (Decision 13A: quota
#   is consumed at token creation and is never released by cancellation).
# - branch scope counts tokens at all counters of that branch; counter
#   scope at that counter; service scope flags that service; org scope the
#   tenant's own tokens. Tenant isolation is absolute: the org filter is
#   always present alongside scope filters.
# - DAILY_TOKEN: source axis filters counted tokens unless the rule's
#   booking_source is ALL (aggregate). A PRIORITY_RESERVE rule at the same
#   (scope_level, scope_id) as an aggregate DAILY_TOKEN rule withholds
#   its `max_count` places from ordinary sources and extends the reserve's
#   own source into the withheld places (CTR-003 source-based allocation).
#   A reserve with no aggregate DAILY_TOKEN rule at its scope has no
#   effect (there is no container to modify).
# - WALK_IN_QUOTA: definitionally limits walk-in issuance; counted tokens
#   always carry booking_source = walk_in, independent of the rule's
#   source axis.
# - PER_USER: counted per authenticated user; guest bookings carry no
#   user identity and are not per-user-limitable.

async def _validate_capacity_rules(
    db: AsyncSession,
    org_id: int,
    counter: Counter,
    service: Optional[Service],
    user_id: Optional[int],
    booking_source: str,
) -> None:
    all_sources = BookingSource.ALL.value

    scope_keys: list[ScheduleScopeLevel] = [ScheduleScopeLevel.ORGANIZATION]
    if counter.branch_id is not None:
        scope_keys.append(ScheduleScopeLevel.BRANCH)
    scope_keys.append(ScheduleScopeLevel.COUNTER)
    if service is not None:
        scope_keys.append(ScheduleScopeLevel.SERVICE)
    scope_ids = {
        ScheduleScopeLevel.ORGANIZATION: org_id,
        ScheduleScopeLevel.BRANCH: counter.branch_id,
        ScheduleScopeLevel.COUNTER: counter.id,
        ScheduleScopeLevel.SERVICE: service.id if service is not None else None,
    }

    rules: list[CapacityRule] = list(
        (
            await db.execute(
                select(CapacityRule).where(
                    CapacityRule.organization_id == org_id,
                    CapacityRule.is_active == True,  # noqa: E712
                    # A reserve whose source differs from the requesting
                    # one still counts (its places are withheld FROM the
                    # requester), so PRIORITY_RESERVE rules of every
                    # source participate in one aggregate scope.
                    or_(
                        CapacityRule.booking_source == all_sources,
                        CapacityRule.booking_source == booking_source,
                        CapacityRule.limit_type == CapacityLimitType.PRIORITY_RESERVE,
                    ),
                    or_(*[
                        and_(
                            CapacityRule.scope_level == level,
                            CapacityRule.scope_id == scope_ids[level],
                        )
                        for level in scope_keys
                    ]),
                )
            )
        ).scalars().all()
    )
    if not rules:
        return  # backward compatible: no rules → identical to pre-0008 behavior

    day_bucket = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    async def _issued_count(rule: CapacityRule) -> Optional[int]:
        """Tokens issued TODAY (UTC calendar day of created_at) inside the
        rule's scope, filtered for the rule's type/source axis. Returns
        None when a rule cannot apply (e.g. per-user for a guest)."""
        stmt = (
            select(func.count())
            .select_from(Token)
            .where(
                Token.organization_id == org_id,
                Token.created_at >= day_bucket,
            )
        )
        if rule.scope_level == ScheduleScopeLevel.BRANCH:
            stmt = stmt.where(
                Token.counter_id.in_(
                    select(Counter.id).where(
                        and_(
                            Counter.organization_id == org_id,
                            Counter.branch_id == rule.scope_id,
                        )
                    )
                )
            )
        elif rule.scope_level == ScheduleScopeLevel.COUNTER:
            stmt = stmt.where(Token.counter_id == rule.scope_id)
        elif rule.scope_level == ScheduleScopeLevel.SERVICE:
            stmt = stmt.where(Token.service_id == rule.scope_id)

        if rule.limit_type == CapacityLimitType.DAILY_TOKEN:
            if rule.booking_source != all_sources:
                stmt = stmt.where(Token.booking_source == rule.booking_source)
        elif rule.limit_type == CapacityLimitType.WALK_IN_QUOTA:
            stmt = stmt.where(Token.booking_source == BookingSource.WALK_IN.value)
        elif rule.limit_type == CapacityLimitType.PER_USER:
            if user_id is None:
                return None  # guests carry no per-user identity
            stmt = stmt.where(Token.user_id == user_id)
            if rule.booking_source != all_sources:
                stmt = stmt.where(Token.booking_source == rule.booking_source)
        return (await db.execute(stmt)).scalar() or 0

    def _scope_label(rule: CapacityRule) -> str:
        level = rule.scope_level
        return _SCOPE_LABELS[level.value if hasattr(level, "value") else level]

    for rule in rules:
        rtype = rule.limit_type

        if rtype == CapacityLimitType.PRIORITY_RESERVE:
            continue  # consumed below by aggregate DAILY_TOKEN checks

        count = await _issued_count(rule)
        if count is None:
            continue

        if rtype == CapacityLimitType.DAILY_TOKEN:
            allowed = rule.max_count
            if rule.booking_source == all_sources:
                # With reserves: ordinary sources lose their places; the
                # reserve's own source may consume the withheld places.
                reserves = [
                    r for r in rules
                    if r.limit_type == CapacityLimitType.PRIORITY_RESERVE
                    and r.scope_level == rule.scope_level
                    and r.scope_id == rule.scope_id
                ]
                if reserves:
                    withheld = sum(
                        r.max_count for r in reserves
                        if r.booking_source != booking_source
                    )
                    allowed = rule.max_count - withheld
            if count >= allowed:
                raise ValueError(
                    f"Daily booking limit reached for this {_scope_label(rule)}."
                )

        elif rtype == CapacityLimitType.WALK_IN_QUOTA:
            # Definitionally limits walk-in issuance: other channels are
            # not part of (and do not exhaust) the walk-in quota.
            if booking_source != BookingSource.WALK_IN.value:
                continue
            if count >= rule.max_count:
                raise ValueError(
                    f"The walk-in quota for this {_scope_label(rule)} "
                    "is exhausted for today."
                )

        elif rtype == CapacityLimitType.PER_USER:
            if count >= rule.max_count:
                raise ValueError(
                    f"You have reached your booking limit for this "
                    f"{_scope_label(rule)} for today."
                )


def _utcnow() -> datetime:
    """Current UTC instant (injectable clock seam — cutoff tests freeze
    this instead of patching datetime itself)."""
    return datetime.now(timezone.utc)


# ── Token sequence ────────────────────────────────────────────────────────────

async def _next_sequence(db: AsyncSession, counter_id: int) -> int:
    """Return the next sequence number for a counter today."""
    from datetime import date
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.coalesce(func.max(Token.sequence), 0)).where(
            and_(Token.counter_id == counter_id, Token.created_at >= today_start)
        )
    )
    return (result.scalar() or 0) + 1


# ── Queue position ─────────────────────────────────────────────────────────────

async def _recalculate_positions(db: AsyncSession, counter_id: int) -> None:
    """Recalculate queue_position for all waiting tokens at a counter."""
    result = await db.execute(
        select(Token).where(
            and_(Token.counter_id == counter_id, Token.status == TokenStatus.WAITING)
        ).order_by(Token.sequence)
    )
    waiting = result.scalars().all()
    for pos, token in enumerate(waiting, start=1):
        token.queue_position = pos
    await db.flush()


# ── Audit helpers ─────────────────────────────────────────────────────────────

async def _record_history(
    db: AsyncSession,
    token: Token,
    old_status: Optional[str],
    new_status: str,
    role: str,
    actor_id: Optional[int],
) -> None:
    db.add(TokenHistory(
        token_id=token.id,
        old_status=old_status,
        new_status=new_status,
        changed_by_role=role,
        changed_by_id=actor_id,
    ))


async def _log_action(
    db: AsyncSession,
    org_id: int,
    counter_id: Optional[int],
    token_id: Optional[int],
    action: str,
    role: str,
    actor_id: Optional[int],
    meta: Optional[dict] = None,
) -> None:
    db.add(QueueLog(
        organization_id=org_id,
        counter_id=counter_id,
        token_id=token_id,
        action=action,
        performed_by_role=role,
        performed_by_id=actor_id,
        meta=json.dumps(meta) if meta else None,
    ))


# ── Slot reservation (Phase 3 Task 3) ────────────────────────────────────────

async def _validate_and_reserve_slot(
    db: AsyncSession,
    organization: Organization,
    counter: Counter,
    slot: Optional[TimeSlot],
    service: Optional[Service] = None,
) -> TimeSlot:
    """Validate a selected time slot against schedules/closures/capacity and
    atomically consume one place (SCH-006).

    Concurrency: the reservation is a guarded conditional UPDATE — the WHERE
    clause re-evaluates `booked_count + held_count < capacity` against the
    CURRENT committed row inside the atomic write, so a concurrent booking
    that consumed the final place makes this UPDATE affect 0 rows. Portable
    across MySQL (InnoDB UPDATE row locking re-reads the committed row) and
    SQLite (serialized writes re-check the guard). `held_count` is included
    for the Decision 14 Phase 5 hold lifecycle.

    The UPDATE runs in the same transaction as the token INSERT: the caller
    commits once, so any later failure rolls back both the reservation and
    the token.
    """
    from app.services import schedule_service

    if slot is None:
        raise ValueError(
            "This counter operates on appointment time slots; select a slot to book."
        )
    if counter.queue_mode != QueueMode.SLOTS:
        raise ValueError("This counter does not support appointment slot bookings.")
    if not slot.is_active:
        raise ValueError("Selected time slot is no longer available.")

    branch = (
        await db.get(Branch, counter.branch_id) if counter.branch_id is not None else None
    )
    today_local = get_tenant_today(organization, branch)
    days_ahead = (slot.slot_date - today_local).days
    if days_ahead < 0:
        raise ValueError("Selected time slot is in the past and can no longer be booked.")
    if days_ahead > MAX_ADVANCE_BOOKING_DAYS:
        raise ValueError(
            f"Bookings are only available up to {MAX_ADVANCE_BOOKING_DAYS} days in advance."
        )

    # Same-day cutoff (SCH-005): a slot whose start instant has already
    # passed is no longer bookable, even though its business date is still
    # today. Slots are stored as the pair (slot_date, UTC time-of-day),
    # which maps to the exact UTC instant (generation fails loudly on UTC
    # date boundaries — OPEN_DECISIONS §4 data-model notes), so the cutoff
    # is instant-based rather than a naive wall-time comparison.
    slot_start_utc = datetime.combine(
        slot.slot_date, slot.start_time, tzinfo=timezone.utc
    )
    if slot_start_utc <= _utcnow():
        raise ValueError(
            "This time slot has already started and can no longer be booked."
        )

    availability = await schedule_service.resolve_day_availability(
        db, organization, slot.slot_date, branch=branch, counter=counter, service=service
    )
    if availability.state == AvailabilityState.FULL:
        raise ValueError("All time slots for this date are fully booked.")
    if availability.state in _SLOT_BLOCKING_STATES:
        raise ValueError(
            availability.reason or "No bookings are being accepted for this date."
        )

    result = await db.execute(
        update(TimeSlot)
        .where(
            TimeSlot.id == slot.id,
            TimeSlot.counter_id == counter.id,
            TimeSlot.organization_id == organization.id,
            TimeSlot.is_active.is_(True),
            TimeSlot.booked_count + TimeSlot.held_count < TimeSlot.capacity,
        )
        .values(booked_count=TimeSlot.booked_count + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ValueError(
            "Selected time slot has just filled up. Please choose another slot."
        )
    await db.refresh(slot)   # reload the updated occupancy for the response
    return slot


# ── Book token ────────────────────────────────────────────────────────────────

async def book_token(
    db: AsyncSession,
    org_id: int,
    counter_id: Optional[int],
    service_id: Optional[int],
    user_id: Optional[int] = None,
    guest_name: Optional[str] = None,
    guest_mobile: Optional[str] = None,
    sub_service_ids: Optional[list[int]] = None,
    form_answers: Optional[dict[str, Any]] = None,
    slot_id: Optional[int] = None,
    booking_source: Optional[Any] = None,
    client_reference: Optional[str] = None,
) -> Token:
    """
    Create a new token and assign queue position.
    Supports:
    - Multi-service counter routing when counter_id is omitted or 0
    - KYC identity verification gating (if service requires KYC)
    - Sub-services pricing and item breakdown (TokenServiceItem)
    - Dynamic custom form immutable snapshotting (TokenFormSnapshot & TokenFormAnswer)
    - Slot-mode booking with atomic capacity reservation (`slot_id`;
      OPEN_DECISIONS Decisions 10/14, SCH-006). Day-queue counters ignore it.
    - SCH-004 capacity-rule enforcement (daily / walk-in quota / per-user /
      priority-reserve limits, source-and-scope aware)

    `booking_source` is set SERVER-SIDE by each channel route (kiosk →
    "kiosk", online → "online") and is never client input; direct service
    calls default to "walk_in" (Decision 13A issuance semantics).
    """
    booking_source = normalize_booking_source(booking_source)
    # KSK-005 / PHY-001: a retried booking (printer failure, network timeout)
    # with the same client reference returns the ORIGINAL token — printing
    # failures must never duplicate a token.
    if client_reference:
        clean_ref = client_reference.strip()
        if clean_ref:
            existing = (
                await db.execute(
                    select(Token).where(
                        and_(
                            Token.organization_id == org_id,
                            Token.client_reference == clean_ref,
                            Token.status != TokenStatus.CANCELLED,
                        )
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        else:
            client_reference = None
    service = None
    if service_id:
        service = await db.get(Service, service_id)
        if not service or service.organization_id != org_id:
            raise ValueError("Service not found")
        # Inactive services are not bookable on any channel (SCH-008
        # eligibility; closes the review-audit gap that previously only
        # surfaced through resolve_day_availability).
        if not service.is_active:
            raise ValueError("This service is currently unavailable")

        # Identity Verification Gating
        if service.requires_identity_verification:
            if not user_id:
                raise ValueError(
                    "Identity verification is required for this service. "
                    "Guest bookings are not permitted."
                )
            from app.services import identity_service
            is_verified = await identity_service.check_user_identity_verified(db, user_id, org_id)
            if not is_verified:
                raise ValueError(
                    "Identity verification required: You must have an approved identity "
                    "verification on file before booking this service."
                )

    # Slot reference validation (slot-mode booking). The slot identifies the
    # counter when counter_id was omitted; otherwise it must match.
    slot: Optional[TimeSlot] = None
    if slot_id is not None:
        slot = await db.get(TimeSlot, slot_id)
        if slot is None or slot.organization_id != org_id:
            raise ValueError("Selected time slot is not available")
        if counter_id and slot.counter_id != counter_id:
            raise ValueError(
                "Selected time slot does not belong to the selected counter"
            )
        counter_id = counter_id or slot.counter_id

    # Multi-service Counter Routing
    if not counter_id:
        if not service_id:
            raise ValueError("Either counter_id or service_id must be provided to book a token.")
        from app.services import catalogue_service
        eligible_counters = await catalogue_service.get_eligible_counters_for_service(
            db, service_id, org_id, branch_id=service.branch_id if service else None
        )
        # Without an explicit slot selection only day-queue counters are
        # auto-routable; slot-mode counters require an explicit slot (Decision 10).
        eligible_counters = [
            c for c in eligible_counters if c.queue_mode == QueueMode.DAY_QUEUE
        ]
        if not eligible_counters:
            raise ValueError("No active counter is currently available for this service.")

        # Route to eligible counter with lowest current waiting count
        best_counter = eligible_counters[0]
        min_waiting = 999999
        for c in eligible_counters:
            waiting_cnt = (await db.execute(
                select(func.count()).select_from(Token).where(
                    and_(Token.counter_id == c.id, Token.status == TokenStatus.WAITING)
                )
            )).scalar() or 0
            if waiting_cnt < min_waiting:
                min_waiting = waiting_cnt
                best_counter = c
        counter = best_counter
        counter_id = counter.id
    else:
        counter = await db.get(Counter, counter_id)
        if not counter or counter.organization_id != org_id:
            raise ValueError("Counter not found")
        if counter.status != CounterStatus.ACTIVE:
            raise ValueError("Counter is not active")

    # Check daily limit
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = (await db.execute(
        select(func.count()).select_from(Token).where(
            and_(Token.counter_id == counter_id, Token.created_at >= today_start)
        )
    )).scalar()
    if today_count >= counter.daily_limit:
        raise ValueError("Counter daily limit reached")

    # SCH-004 capacity rules: layered independent checks (daily-token,
    # walk-in quota, per-user, priority reserves) before the reservation —
    # a rejected booking must not consume slot occupancy.
    await _validate_capacity_rules(
        db, org_id, counter, service, user_id, booking_source
    )

    # Atomic slot reservation (slot-mode counters only; day-queue skips).
    # Runs inside the same transaction as the token insert below: one commit
    # finalizes both, and any failure rolls back reservation + token together.
    if counter.queue_mode == QueueMode.SLOTS or slot is not None:
        organization = await db.get(Organization, org_id)
        slot = await _validate_and_reserve_slot(
            db, organization, counter, slot, service=service
        )

    seq = await _next_sequence(db, counter_id)
    token_number = f"{counter.token_prefix}-{seq:03d}"

    # Calculate pricing rollup for token snapshot
    amount_paid = service.price if service else Decimal("0.00")
    selected_subs: list[SubService] = []
    if sub_service_ids:
        subs_stmt = select(SubService).where(
            SubService.id.in_(sub_service_ids),
            SubService.is_active.is_(True),
        )
        subs_res = await db.execute(subs_stmt)
        selected_subs = list(subs_res.scalars().all())
        for s in selected_subs:
            amount_paid += s.additional_price

    token = Token(
        organization_id=org_id,
        user_id=user_id,
        counter_id=counter_id,
        service_id=service_id,
        slot_id=slot.id if slot is not None else None,
        booking_source=booking_source,
        token_number=token_number,
        sequence=seq,
        status=TokenStatus.WAITING,
        guest_name=guest_name,
        guest_mobile=guest_mobile,
        amount_paid=amount_paid,
        booked_at=datetime.now(timezone.utc),
        client_reference=client_reference,
    )
    db.add(token)
    try:
        await db.flush()   # get token.id
    except IntegrityError:
        # Uniqueness/FK conflict (e.g. concurrent sequence race or a slot
        # deleted mid-booking). Roll back the reservation + token together
        # and report a safe, non-leaking message (API-005).
        await db.rollback()
        logger.warning("Token booking conflict for org {} — rolled back", org_id)
        raise ValueError(
            "Booking could not be completed due to a temporary conflict. "
            "Please try again."
        )

    # Record TokenServiceItem breakdown
    if service_id and service:
        db.add(TokenServiceItem(
            token_id=token.id,
            service_id=service_id,
            sub_service_id=None,
            price=service.price,
            estimated_minutes=service.estimated_minutes,
            sequence_order=1,
        ))
        for idx, sub_item in enumerate(selected_subs, start=2):
            db.add(TokenServiceItem(
                token_id=token.id,
                service_id=service_id,
                sub_service_id=sub_item.id,
                price=sub_item.additional_price,
                estimated_minutes=sub_item.additional_minutes,
                sequence_order=idx,
            ))

    # Form Snapshotting
    if service_id:
        from app.services import form_service
        active_form = await form_service.get_active_form_for_service(db, service_id)
        if active_form:
            await form_service.create_token_form_snapshot(
                db, token.id, active_form, form_answers or {}
            )

    # Generate QR code
    qr_path = await qr_generator.generate_token_qr(
        token_id=token.id,
        org_id=org_id,
        counter_id=counter_id,
        user_id=user_id,
    )
    token.qr_code_path = qr_path

    await _recalculate_positions(db, counter_id)
    await _record_history(db, token, None, TokenStatus.WAITING, "system", None)
    await _log_action(
        db, org_id, counter_id, token.id, "token_created", "system", user_id,
        meta={"slot_id": slot.id} if slot is not None else None,
    )

    try:
        await db.commit()
    except IntegrityError:
        # Commit-time uniqueness/FK conflict (MySQL defers some checks to
        # commit). Roll back everything — reservation included — atomically.
        await db.rollback()
        logger.warning("Token booking commit conflict for org {} — rolled back", org_id)
        raise ValueError(
            "Booking could not be completed due to a temporary conflict. "
            "Please try again."
        )
    await db.refresh(token)
    logger.info("Token {} created for org {}", token.token_number, org_id)
    return token


# ── Token actions (called by organization operators) ──────────────────────────

_VALID_TRANSITIONS: dict[str, list[str]] = {
    "call":     [TokenStatus.WAITING, TokenStatus.ON_HOLD],
    "recall":   [TokenStatus.CALLED],
    "hold":     [TokenStatus.CALLED, TokenStatus.IN_PROGRESS],
    "progress": [TokenStatus.CALLED],
    "complete": [TokenStatus.CALLED, TokenStatus.IN_PROGRESS],
    "skip":     [TokenStatus.WAITING, TokenStatus.CALLED],
    "cancel":   [TokenStatus.WAITING, TokenStatus.CALLED, TokenStatus.ON_HOLD],
}

_ACTION_STATUS_MAP: dict[str, str] = {
    "call":     TokenStatus.CALLED,
    "recall":   TokenStatus.CALLED,
    "hold":     TokenStatus.ON_HOLD,
    "progress": TokenStatus.IN_PROGRESS,
    "complete": TokenStatus.COMPLETED,
    "skip":     TokenStatus.SKIPPED,
    "cancel":   TokenStatus.CANCELLED,
}


async def perform_token_action(
    db: AsyncSession,
    token_id: int,
    org_id: int,
    action: str,
    actor_role: str = "organization",
    actor_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> Token:
    token = await db.get(Token, token_id)
    if not token or token.organization_id != org_id:
        raise ValueError("Token not found")

    # SHIFT-003: An employee cannot change queue state without an active approved shift
    if actor_role == "employee" and actor_id:
        from app.services.shift_service import require_active_shift
        await require_active_shift(db, org_id, token.counter_id, actor_id)

    if action not in _VALID_TRANSITIONS:
        raise ValueError(f"Unknown action: {action}")

    # FIN-001: an admin payment hold blocks operators from advancing a
    # paused paid booking through ANY queue endpoint (screen-end enforced
    # in views, engine-end enforced here — bypass prevention).
    if token.payment_hold and action in ("call", "progress", "complete"):
        from app.services import payment_policy_service
        raise PermissionError(
            "This booking is paused under a payment hold; a finance admin must resume it first"
        )

    if token.status not in _VALID_TRANSITIONS[action]:
        raise ValueError(
            f"Cannot {action} a token with status '{token.status}'. "
            f"Expected one of: {_VALID_TRANSITIONS[action]}"
        )

    old_status = token.status
    new_status = _ACTION_STATUS_MAP[action]
    token.status = new_status
    if notes:
        token.notes = notes

    now = datetime.now(timezone.utc)
    if action == "call":
        token.called_at = now
    elif action in ("complete", "skip"):
        token.completed_at = now

    await _recalculate_positions(db, token.counter_id)
    await _record_history(db, token, old_status, new_status, actor_role, actor_id)
    await _log_action(
        db, org_id, token.counter_id, token.id,
        f"token_{action}d", actor_role, actor_id,
        meta={"old": old_status, "new": new_status},
    )

    await db.commit()
    await db.refresh(token)
    return token


# ── Live tracking ─────────────────────────────────────────────────────────────

async def get_token_tracking(db: AsyncSession, token_id: int) -> dict:
    """Compute live tracking data for a token."""
    token = await db.get(Token, token_id)
    if not token:
        raise ValueError("Token not found")

    counter = await db.get(Counter, token.counter_id)
    service = await db.get(Service, token.service_id) if token.service_id else None
    est_minutes = service.estimated_minutes if service else 10

    # People ahead = tokens waiting with lower sequence at same counter
    people_ahead = (await db.execute(
        select(func.count()).select_from(Token).where(
            and_(
                Token.counter_id == token.counter_id,
                Token.status == TokenStatus.WAITING,
                Token.sequence < token.sequence,
            )
        )
    )).scalar()

    # Current token being served
    current_result = await db.execute(
        select(Token.token_number).where(
            and_(
                Token.counter_id == token.counter_id,
                Token.status.in_([TokenStatus.CALLED, TokenStatus.IN_PROGRESS]),
            )
        ).order_by(Token.called_at.desc()).limit(1)
    )
    current_token = current_result.scalar_one_or_none()

    return {
        "token_id": token.id,
        "token_number": token.token_number,
        "status": token.status,
        "queue_position": token.queue_position,
        "people_ahead": int(people_ahead),
        "estimated_wait_minutes": int(people_ahead) * est_minutes,
        "current_token_at_counter": current_token,
        "counter_name": counter.counter_name if counter else "",
    }


# ── Public display data ────────────────────────────────────────────────────────

async def get_display_data(db: AsyncSession, org_id: int) -> dict:
    """Data for the public display screen."""
    counters_result = await db.execute(
        select(Counter).where(
            and_(Counter.organization_id == org_id, Counter.status == CounterStatus.ACTIVE)
        ).order_by(Counter.counter_number)
    )
    counters = counters_result.scalars().all()

    counter_data = []
    for c in counters:
        current = (await db.execute(
            select(Token.token_number).where(
                and_(
                    Token.counter_id == c.id,
                    Token.status.in_([TokenStatus.CALLED, TokenStatus.IN_PROGRESS]),
                )
            ).order_by(Token.called_at.desc()).limit(1)
        )).scalar_one_or_none()

        upcoming_result = await db.execute(
            select(Token.token_number).where(
                and_(Token.counter_id == c.id, Token.status == TokenStatus.WAITING)
            ).order_by(Token.sequence).limit(5)
        )
        upcoming = upcoming_result.scalars().all()

        counter_data.append({
            "counter_id": c.id,
            "counter_name": c.counter_name,
            "counter_number": c.counter_number,
            "status": c.status,
            "current_token": current,
            "upcoming_tokens": list(upcoming),
        })

    # Announcement
    from sqlalchemy import and_ as _and
    from models.setting import Setting
    ann_result = await db.execute(
        select(Setting.value).where(
            _and(Setting.organization_id == org_id, Setting.key == "announcement_text")
        )
    )
    announcement = ann_result.scalar_one_or_none() or ""

    return {"counters": counter_data, "announcement": announcement}
