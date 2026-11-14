"""
app/services/wait_time_service.py
─────────────────────────────────
Phase 10 (MASTER §16, §21, §23): the single authoritative, explainable
waiting-time estimation engine.

Design contract
───────────────
- ONE source of truth: every consumer (live tracking API, customer portal,
  public display, PDF snapshot, AI assistant tool, reports) reads through
  this service — no consumer re-implements queue math (MASTER §2.2).
- EXPLAINABLE (§16.3): every estimate carries a human-readable explanation
  naming its raw inputs (tokens ahead × per-person service duration,
  divided across active counters).
- HONEST (§16.1, §24.7): with neither recent completion history nor an
  explicitly configured service duration, the estimate is
  `insufficient_data` — `None` values, never a fabricated 10-minute
  default.
- NOT AI-GENERATED (§16.3, §45 line 2 of the rules): pure deterministic
  statistics over real queue/history rows. The AI assistant only relays
  these verified numbers.
- SNAPSHOT + ACCURACY (§16.5, §16.6): estimates are persisted as
  append-only snapshots (initial at booking, recalcs at queue events) and
  compared against the actual service start (`tokens.called_at`) for
  accuracy metrics in role-scoped reports (§21).

Estimator inputs (§16.2 subset actually available in the schema):
  - number of eligible tokens ahead (WAITING, same counter, lower sequence —
    skips/cancels/no-shows drop out by status; recalls keep queue order)
  - active counters serving the same service (ACTIVE status; breaks/
    closures excluded) → multi-counter division of the line load
  - blended per-person service duration: rolling median of recent real
    completion durations (called_at → completed_at), blended with the
    configured `services.estimated_minutes` while history is thin
  - current service progress: the token being served is never counted as
    "ahead" (§16.2)
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.counter import Counter, CounterStatus
from models.service import Service
from models.token import Token, TokenStatus
from models.wait_time_estimate import (
    WaitTimeEstimate as WaitTimeEstimateModel,
)

# §16.2/§16.3 rolling window: recent completions only — stale history would
# misrepresent today's counter speed.
HISTORY_WINDOW_DAYS = 30
# Maximum recent durations considered (rolling median over the tail).
HISTORY_SAMPLE_LIMIT = 20
# Minimum completed-service samples before pure-historical estimation.
HISTORICAL_MIN_SAMPLES = 5
# While 0 < samples < HISTORICAL_MIN_SAMPLES, blend with the configured
# duration; the historical weight grows as evidence accumulates.
BLEND_WEIGHT_DENOMINATOR = 3
# Recalc snapshots recorded per material queue event (bounded write amplification).
RECALC_TOKEN_LIMIT = 3


@dataclass
class WaitTimeEstimate:
    """An explainable waiting-time estimate (or an honest refusal)."""

    estimated_minutes: Optional[int]
    range_min: Optional[int]
    range_max: Optional[int]
    status: str                 # "estimate" | "insufficient_data"
    basis: str                  # "historical" | "blended" | "configured" | "insufficient_data"
    people_ahead: int
    people_ahead_effective: int
    active_counter_count: int
    duration_sample_count: int
    duration_minutes: Optional[float]   # blended per-person service duration
    estimated_at: datetime              # UTC point-in-time (§16.4/§16.5)
    explanation: str                    # human-readable basis (§16.3)

    def serialize(self) -> dict:
        """JSON-safe payload for APIs / WebSocket / AI tool output."""
        return {
            "estimated_wait_minutes": self.estimated_minutes,
            "estimated_wait_range_min": self.range_min,
            "estimated_wait_range_max": self.range_max,
            "estimated_wait_status": self.status,
            "estimated_wait_basis": self.basis,
            "people_ahead": self.people_ahead,
            "people_ahead_effective": self.people_ahead_effective,
            "active_counter_count": self.active_counter_count,
            "duration_sample_count": self.duration_sample_count,
            "estimated_at": self.estimated_at.isoformat().replace("+00:00", "Z"),
            "estimate_explanation": self.explanation,
        }


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _fmt_minutes(value: float) -> str:
    """Display minutes without a spurious trailing .0 (6.0 → '6')."""
    if abs(value - round(value)) < 0.05:
        return str(int(round(value)))
    return f"{value:.1f}"


def _variability(values: list[float], point: float) -> float:
    """Uncertainty (minutes) around the blended per-person duration."""
    if len(values) >= 2:
        return max(1.0, statistics.pstdev(values))
    # Thin evidence → proportional band, floored at 1 minute.
    return max(1.0, 0.25 * point)


async def _people_ahead(db: AsyncSession, token: Token) -> int:
    """Eligible tokens ahead: WAITING, same counter, lower sequence.

    Priority/order rules, skips, cancellations and recalls are already
    reflected in `sequence` / `status` by the token engine, so counting
    WAITING rows with a lower sequence IS the priority-aware, skip-aware
    count (§16.2). The token currently being served is not ahead of anyone.
    """
    result = await db.execute(
        select(func.count()).select_from(Token).where(
            and_(
                Token.counter_id == token.counter_id,
                Token.status == TokenStatus.WAITING,
                Token.sequence < token.sequence,
            )
        )
    )
    return int(result.scalar() or 0)


async def _active_counter_count(db: AsyncSession, token: Token) -> int:
    """ACTIVE counters serving the token's service (breaks/closures excluded).

    Falls back to 1 (the token's own counter) when the token has no service
    — legacy counter-bound queues stay per-counter.
    """
    if token.service_id:
        result = await db.execute(
            select(func.count()).select_from(Counter).where(
                and_(
                    Counter.organization_id == token.organization_id,
                    Counter.service_id == token.service_id,
                    Counter.status == CounterStatus.ACTIVE,
                )
            )
        )
        count = int(result.scalar() or 0)
        if count:
            return count
    own = await db.get(Counter, token.counter_id)
    return 1 if (own and own.status == CounterStatus.ACTIVE) else 0


async def _own_counter_active(db: AsyncSession, token: Token) -> bool:
    own = await db.get(Counter, token.counter_id)
    return bool(own and own.status == CounterStatus.ACTIVE)


async def _recent_service_durations(db: AsyncSession, service_id: int) -> list[float]:
    """Recent real service durations (called_at → completed_at) in minutes."""
    since = datetime.now(timezone.utc) - timedelta(days=HISTORY_WINDOW_DAYS)
    result = await db.execute(
        select(Token.called_at, Token.completed_at)
        .where(
            and_(
                Token.service_id == service_id,
                Token.status == TokenStatus.COMPLETED,
                Token.called_at.is_not(None),
                Token.completed_at.is_not(None),
                Token.completed_at >= since,
            )
        )
        .order_by(Token.completed_at.desc())
        .limit(HISTORY_SAMPLE_LIMIT)
    )
    durations = []
    for called_at, completed_at in result.all():
        # Defensive: tz-naive MySQL columns are treated as UTC.
        start = called_at if called_at.tzinfo else called_at.replace(tzinfo=timezone.utc)
        end = completed_at if completed_at.tzinfo else completed_at.replace(tzinfo=timezone.utc)
        minutes = (end - start).total_seconds() / 60.0
        if minutes >= 0:
            durations.append(minutes)
    return durations


async def estimate_for_token(db: AsyncSession, token: Token) -> WaitTimeEstimate:
    """Compute the current explainable estimate for one token."""
    now = datetime.now(timezone.utc)

    ahead = await _people_ahead(db, token)
    active_counters = await _active_counter_count(db, token)
    own_active = await _own_counter_active(db, token)

    durations = (
        await _recent_service_durations(db, token.service_id)
        if token.service_id else []
    )
    configured = 0.0
    if token.service_id:
        service = await db.get(Service, token.service_id)
        if service is not None:
            configured = float(service.estimated_minutes or 0)

    sample_count = len(durations)

    # ── Blended per-person service duration (§16.2/§16.3) ─────────────────
    if sample_count >= HISTORICAL_MIN_SAMPLES:
        per_person = _median(durations)
        basis = "historical"
    elif sample_count > 0 and configured > 0:
        weight = sample_count / (sample_count + BLEND_WEIGHT_DENOMINATOR)
        per_person = weight * _median(durations) + (1 - weight) * configured
        basis = "blended"
    elif sample_count > 0:
        per_person = _median(durations)
        basis = "historical"
    elif configured > 0:
        per_person = configured
        basis = "configured"
    else:
        # §16.1/§16.3/§24.7 — no real data, no configured duration:
        # refuse to invent precision.
        return WaitTimeEstimate(
            estimated_minutes=None, range_min=None, range_max=None,
            status="insufficient_data", basis="insufficient_data",
            people_ahead=ahead, people_ahead_effective=ahead,
            active_counter_count=active_counters,
            duration_sample_count=0, duration_minutes=None,
            estimated_at=now,
            explanation=(
                "Not enough data yet: no recent completed services for this "
                "service and no configured service duration, so an honest "
                "waiting-time estimate is not available."
            ),
        )

    # Guard against degenerate data (e.g. all-zero durations).
    per_person = max(0.0, per_person)
    if per_person <= 0:
        return WaitTimeEstimate(
            estimated_minutes=None, range_min=None, range_max=None,
            status="insufficient_data", basis="insufficient_data",
            people_ahead=ahead, people_ahead_effective=ahead,
            active_counter_count=active_counters,
            duration_sample_count=sample_count, duration_minutes=None,
            estimated_at=now,
            explanation=(
                "Recent service data is degenerate (zero durations), so an "
                "honest waiting-time estimate is not available."
            ),
        )

    # ── Multi-counter division of the line load (§16.2) ───────────────────
    effective_ahead = (
        math.ceil(ahead / active_counters) if active_counters > 1 else ahead
    )
    wait_minutes = effective_ahead * per_person

    variability = _variability(durations, per_person)
    range_min = max(0, int(math.floor(wait_minutes - variability)))
    range_max = int(math.ceil(wait_minutes + variability))

    duration_word = "median" if basis != "configured" else "configured"
    per_person_disp = _fmt_minutes(per_person)
    explanation = (
        f"{ahead} token{'s' if ahead != 1 else ''} ahead across "
        f"{max(active_counters, 1)} active counter{'s' if active_counters != 1 else ''} "
        f"(~{per_person_disp} min {duration_word} service time, "
        f"{sample_count if sample_count else 'no'} recent sample"
        f"{'s' if sample_count != 1 else ''})"
    )
    if not own_active:
        explanation += " — your counter is currently on break/closed, which may add delay"

    return WaitTimeEstimate(
        estimated_minutes=int(round(wait_minutes)),
        range_min=range_min,
        range_max=range_max,
        status="estimate",
        basis=basis,
        people_ahead=ahead,
        people_ahead_effective=effective_ahead,
        active_counter_count=active_counters,
        duration_sample_count=sample_count,
        duration_minutes=round(per_person, 2),
        estimated_at=now,
        explanation=explanation,
    )


async def estimate_for_counter_queue(db: AsyncSession, counter: Counter) -> dict:
    """Estimate for the PUBLIC display (§16.4/§16.7): the wait a NEW arrival
    would face at this counter right now.

    Same honest insufficient-data rule as `estimate_for_token`. The estimate
    covers everyone currently WAITING at the counter (they all stand between
    a new arrival and service), divided across the active counters serving
    the counter's service. Returns the same serialized shape as
    `WaitTimeEstimate.serialize()` plus nothing else — token numbers only,
    never PII.
    """
    now = datetime.now(timezone.utc)

    waiting_count = int((await db.execute(
        select(func.count()).select_from(Token).where(
            and_(
                Token.counter_id == counter.id,
                Token.status == TokenStatus.WAITING,
            )
        )
    )).scalar() or 0)

    service_id = counter.service_id
    if not service_id:
        # Counters are not always service-bound (day-queue mode): derive the
        # service from the NEXT waiting token's booking, which is exactly
        # the service a new arrival would queue behind.
        service_id = (await db.execute(
            select(Token.service_id).where(
                and_(
                    Token.counter_id == counter.id,
                    Token.status == TokenStatus.WAITING,
                    Token.service_id.is_not(None),
                )
            ).order_by(Token.sequence).limit(1)
        )).scalar()
    durations = (
        await _recent_service_durations(db, service_id) if service_id else []
    )
    configured = 0.0
    if service_id:
        service = await db.get(Service, service_id)
        if service is not None:
            configured = float(service.estimated_minutes or 0)

    if service_id:
        active_counters = int((await db.execute(
            select(func.count()).select_from(Counter).where(
                and_(
                    Counter.organization_id == counter.organization_id,
                    Counter.service_id == service_id,
                    Counter.status == CounterStatus.ACTIVE,
                )
            )
        )).scalar() or 0) or 1
    else:
        active_counters = 1

    sample_count = len(durations)
    insufficient_explanation = (
        "Not enough data yet: no recent completed services and no configured "
        "service duration, so an honest waiting-time estimate is not "
        "available."
    )

    def _refusal() -> dict:
        return {
            "estimated_wait_minutes": None,
            "estimated_wait_range_min": None,
            "estimated_wait_range_max": None,
            "estimated_wait_status": "insufficient_data",
            "estimated_wait_basis": "insufficient_data",
            "people_waiting": waiting_count,
            "active_counter_count": active_counters,
            "duration_sample_count": 0,
            "estimated_at": now.isoformat().replace("+00:00", "Z"),
            "estimate_explanation": insufficient_explanation,
        }

    if sample_count >= HISTORICAL_MIN_SAMPLES:
        per_person = _median(durations)
        basis = "historical"
    elif sample_count > 0 and configured > 0:
        weight = sample_count / (sample_count + BLEND_WEIGHT_DENOMINATOR)
        per_person = weight * _median(durations) + (1 - weight) * configured
        basis = "blended"
    elif sample_count > 0:
        per_person = _median(durations)
        basis = "historical"
    elif configured > 0:
        per_person = configured
        basis = "configured"
    else:
        return _refusal()

    per_person = max(0.0, per_person)
    if per_person <= 0:
        return _refusal()

    effective_waiting = (
        math.ceil(waiting_count / active_counters) if active_counters > 1
        else waiting_count
    )
    wait_minutes = effective_waiting * per_person
    variability = _variability(durations, per_person)

    duration_word = "median" if basis != "configured" else "configured"
    explanation = (
        f"{waiting_count} waiting across "
        f"{active_counters} active counter{'s' if active_counters != 1 else ''} "
        f"(~{_fmt_minutes(per_person)} min {duration_word} service time)"
    )

    return {
        "estimated_wait_minutes": int(round(wait_minutes)),
        "estimated_wait_range_min": max(0, int(math.floor(wait_minutes - variability))),
        "estimated_wait_range_max": int(math.ceil(wait_minutes + variability)),
        "estimated_wait_status": "estimate",
        "estimated_wait_basis": basis,
        "people_waiting": waiting_count,
        "active_counter_count": active_counters,
        "duration_sample_count": sample_count,
        "estimated_at": now.isoformat().replace("+00:00", "Z"),
        "estimate_explanation": explanation,
    }


# ── §16.6 persistence ─────────────────────────────────────────────────────────
def _snapshot_row(token: Token, est: WaitTimeEstimate, kind: str) -> WaitTimeEstimateModel:
    return WaitTimeEstimateModel(
        organization_id=token.organization_id,
        token_id=token.id,
        counter_id=token.counter_id,
        snapshot_kind=kind,
        predicted_wait_minutes=est.estimated_minutes,
        predicted_range_min=est.range_min,
        predicted_range_max=est.range_max,
        estimation_status=est.status,
        estimation_basis=est.basis,
        estimation_explanation=est.explanation[:500],
        people_ahead=est.people_ahead,
        active_counter_count=est.active_counter_count,
        duration_sample_count=est.duration_sample_count,
        estimated_at=est.estimated_at,
    )


async def record_initial_snapshot(db: AsyncSession, token: Token) -> None:
    """Persist the §16.6 anchor prediction at token creation.

    Runs INSIDE the booking transaction; failures are logged and swallowed —
    a snapshot problem must never fail a booking (B15 guarantee precedent).
    """
    try:
        est = await estimate_for_token(db, token)
        db.add(_snapshot_row(token, est, "initial"))
    except Exception:
        logger.exception("Wait-time initial snapshot failed for token {}", token.id)


async def record_recalc_for_counter(db: AsyncSession, counter_id: int,
                                    organization_id: int) -> None:
    """Persist recalc snapshots for the first waiting tokens after a
    material queue event (call/complete/skip/cancel/hold — §16.6)."""
    try:
        waiting = (await db.execute(
            select(Token)
            .where(
                and_(
                    Token.counter_id == counter_id,
                    Token.organization_id == organization_id,
                    Token.status == TokenStatus.WAITING,
                )
            )
            .order_by(Token.sequence)
            .limit(RECALC_TOKEN_LIMIT)
        )).scalars().all()
        for token in waiting:
            est = await estimate_for_token(db, token)
            db.add(_snapshot_row(token, est, "recalc"))
    except Exception:
        logger.exception(
            "Wait-time recalc snapshots failed for counter {}", counter_id
        )


# ── §16.6 / §21 accuracy reporting ────────────────────────────────────────────

async def accuracy_summary(db: AsyncSession, organization_id: Optional[int] = None,
                           days: int = 30) -> dict:
    """Predicted-vs-actual wait accuracy over a rolling window.

    Compares each token's latest `initial` prediction against its measured
    actual wait (booked_at → called_at, the real service start, §16.6).
    Tokens whose honest estimate was `insufficient_data` are reported
    separately — they are unmeasurable predictions, never fabricated ones.

    Role scoping (§21): callers pass an organization_id for org-scoped
    reports; platform admins omit it for the platform-wide view.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Latest initial snapshot per token that carries a real prediction.
    latest_initial = (
        select(func.max(WaitTimeEstimateModel.id))
        .where(
            and_(
                WaitTimeEstimateModel.snapshot_kind == "initial",
                WaitTimeEstimateModel.predicted_wait_minutes.is_not(None),
            )
        )
        .group_by(WaitTimeEstimateModel.token_id)
        .scalar_subquery()
    )

    conditions = [
        WaitTimeEstimateModel.id.in_(latest_initial),
        Token.called_at.is_not(None),
        func.coalesce(Token.booked_at, Token.created_at) >= cutoff,
    ]
    if organization_id is not None:
        conditions.append(WaitTimeEstimateModel.organization_id == organization_id)

    result = await db.execute(
        select(WaitTimeEstimateModel.predicted_wait_minutes, Token)
        .join(Token, Token.id == WaitTimeEstimateModel.token_id)
        .where(and_(*conditions))
    )
    rows = result.all()

    errors: list[float] = []
    actuals: list[float] = []
    predicted_list: list[float] = []
    for predicted, token in rows:
        booked = token.booked_at or token.created_at
        if booked is None or token.called_at is None:
            continue
        booked = booked if booked.tzinfo else booked.replace(tzinfo=timezone.utc)
        called = token.called_at if token.called_at.tzinfo else token.called_at.replace(tzinfo=timezone.utc)
        actual = (called - booked).total_seconds() / 60.0
        if actual < 0:
            continue
        actuals.append(actual)
        predicted_list.append(float(predicted))
        errors.append(float(predicted) - actual)  # + = over-estimate

    # Unmeasurable-side honesty: how many initial estimates could not even
    # be produced (insufficient data) in the window.
    no_data_conditions = [
        WaitTimeEstimateModel.snapshot_kind == "initial",
        WaitTimeEstimateModel.estimation_status == "insufficient_data",
        func.coalesce(Token.booked_at, Token.created_at) >= cutoff,
    ]
    if organization_id is not None:
        no_data_conditions.append(
            WaitTimeEstimateModel.organization_id == organization_id
        )
    insufficient = int((await db.execute(
        select(func.count())
        .select_from(WaitTimeEstimateModel)
        .join(Token, Token.id == WaitTimeEstimateModel.token_id)
        .where(and_(*no_data_conditions))
    )).scalar() or 0)

    sample_count = len(errors)
    return {
        "window_days": days,
        "sample_count": sample_count,
        "measured_avg_actual_wait_minutes": (
            round(sum(actuals) / sample_count, 1) if sample_count else None
        ),
        "estimated_avg_predicted_wait_minutes": (
            round(sum(predicted_list) / sample_count, 1) if sample_count else None
        ),
        "mae_minutes": (
            round(sum(abs(e) for e in errors) / sample_count, 1) if sample_count else None
        ),
        "bias_minutes": (
            round(sum(errors) / sample_count, 1) if sample_count else None
        ),  # positive = system over-estimates wait
        "insufficient_data_estimates": insufficient,
    }
