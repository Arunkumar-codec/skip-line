"""
tests/unit/test_wait_time_service.py
------------------------------------
Phase 10 (MASTER 16.1-16.4, 16.6): unit tests for the authoritative
explainable waiting-time estimator.

Covers:
- 16.1/16.3/24.7 insufficient-data honesty (None, never a fake 10)
- configured-duration fallback and basis labeling
- historical rolling-median estimation + thin-sample blending
- multi-counter division of the line load
- skip/cancel/recall state exclusion (priority-aware people-ahead count)
- counter break/closure surfacing in the explanation
- timestamp generation (16.4) and serializer shape
- 16.6 snapshot persistence + accuracy metrics (MAE/bias, measured vs
  estimated kept distinct)
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import and_, select

from app.services import wait_time_service as wts
from models.counter import Counter, CounterStatus
from models.organization import Organization
from models.service import Service
from models.token import Token, TokenStatus
from models.wait_time_estimate import WaitTimeEstimate


async def _make_org(db) -> Organization:
    stamp = datetime.now(timezone.utc).timestamp()
    org = Organization(
        name=f"WTOrg {stamp}",
        registration_number=f"WTE-{stamp}",
        org_type="hospital",
        address="Test Address",
        city="Test City",
        state="Test State",
        email=f"wte{stamp}@test.example",
        mobile="9000000000",
        password_hash="x",
    )
    db.add(org)
    await db.flush()
    return org


async def _make_counter(db, org_id, number=1, status=CounterStatus.ACTIVE) -> Counter:
    counter = Counter(
        organization_id=org_id,
        counter_name=f"Counter {number}",
        counter_number=number,
        token_prefix="W",
        status=status,
    )
    db.add(counter)
    await db.flush()
    return counter


async def _make_service(db, org_id, estimated_minutes=0) -> Service:
    service = Service(
        organization_id=org_id,
        name=f"WTE Svc {datetime.now(timezone.utc).timestamp()}",
        price=0,
        is_active=True,
        estimated_minutes=estimated_minutes,
    )
    db.add(service)
    await db.flush()
    return service


async def _make_token(db, org, counter, service=None, sequence=1,
                      status=TokenStatus.WAITING, booked_at=None,
                      called_at=None, completed_at=None) -> Token:
    token = Token(
        organization_id=org.id,
        counter_id=counter.id,
        service_id=service.id if service else None,
        token_number=f"W-{sequence:03d}",
        sequence=sequence,
        status=status,
        queue_position=sequence,
        booked_at=booked_at or datetime.now(timezone.utc),
        called_at=called_at,
        completed_at=completed_at,
    )
    db.add(token)
    await db.flush()
    return token


@pytest.fixture
async def org(db_session):
    return await _make_org(db_session)


# -- 16.1/16.3/24.7 insufficient-data honesty --------------------------------

class TestInsufficientDataHonesty:
    async def test_no_history_no_config_returns_none_never_ten(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=0)
        token = await _make_token(db_session, org, counter, service)

        est = await wts.estimate_for_token(db_session, token)
        assert est.status == "insufficient_data"
        assert est.estimated_minutes is None
        assert est.range_min is None
        assert est.range_max is None
        assert est.basis == "insufficient_data"
        assert "Not enough data" in est.explanation

    async def test_serialized_refusal_is_none_not_default_ten(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=0)
        token = await _make_token(db_session, org, counter, service)

        payload = (await wts.estimate_for_token(db_session, token)).serialize()
        assert payload["estimated_wait_minutes"] is None
        assert payload["estimated_wait_status"] == "insufficient_data"
        assert payload["estimate_explanation"]

    async def test_zero_duration_history_is_degenerate_not_zero_estimate(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=0)
        base = datetime.now(timezone.utc) - timedelta(hours=1)
        for seq in range(1, 7):
            await _make_token(
                db_session, org, counter, service, sequence=seq,
                status=TokenStatus.COMPLETED,
                called_at=base, completed_at=base,  # zero-duration rows
            )
        token = await _make_token(db_session, org, counter, service, sequence=10)

        est = await wts.estimate_for_token(db_session, token)
        assert est.status == "insufficient_data"
        assert est.estimated_minutes is None


# -- 16.2/16.3 estimate computation -------------------------------------------

class TestEstimateComputation:
    async def test_configured_basis_counts_ahead_times_duration(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=10)
        await _make_token(db_session, org, counter, service, sequence=1)
        await _make_token(db_session, org, counter, service, sequence=2)
        token = await _make_token(db_session, org, counter, service, sequence=3)

        est = await wts.estimate_for_token(db_session, token)
        assert est.status == "estimate"
        assert est.basis == "configured"
        assert est.people_ahead == 2
        assert est.estimated_minutes == 20
        assert est.range_min is not None and est.range_max is not None
        assert est.range_min <= 20 <= est.range_max

    async def test_explanation_mentions_inputs(self, db_session, org):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=6)
        await _make_token(db_session, org, counter, service, sequence=1)
        token = await _make_token(db_session, org, counter, service, sequence=2)

        est = await wts.estimate_for_token(db_session, token)
        assert "1 token ahead" in est.explanation
        assert "1 active counter" in est.explanation
        assert "~6 min" in est.explanation

    async def test_multi_counter_divides_line_load(self, db_session, org):
        # 2 ACTIVE counters for the same service, 3 tokens ahead ->
        # effective load = ceil(3/2) = 2 (16.2 active-counter input).
        service = await _make_service(db_session, org.id, estimated_minutes=10)
        c1 = await _make_counter(db_session, org.id, number=1)
        c2 = await _make_counter(db_session, org.id, number=2)
        c1.service_id = service.id
        c2.service_id = service.id
        await db_session.flush()
        for seq in (1, 2, 3):
            await _make_token(db_session, org, c1, service, sequence=seq)
        token = await _make_token(db_session, org, c1, service, sequence=4)

        est = await wts.estimate_for_token(db_session, token)
        assert est.active_counter_count == 2
        assert est.people_ahead_effective == 2  # ceil(3/2)
        assert est.estimated_minutes == 20

    async def test_break_counter_surfaced_in_explanation(self, db_session, org):
        counter = await _make_counter(
            db_session, org.id, number=1, status=CounterStatus.BREAK
        )
        service = await _make_service(db_session, org.id, estimated_minutes=10)
        token = await _make_token(db_session, org, counter, service)

        est = await wts.estimate_for_token(db_session, token)
        assert "break" in est.explanation.lower() or "closed" in est.explanation.lower()

    async def test_skip_and_cancelled_tokens_not_counted_ahead(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=10)
        await _make_token(db_session, org, counter, service, sequence=1,
                          status=TokenStatus.SKIPPED)
        await _make_token(db_session, org, counter, service, sequence=2,
                          status=TokenStatus.CANCELLED)
        # A recall keeps a called token out of the "ahead" count too.
        await _make_token(db_session, org, counter, service, sequence=3,
                          status=TokenStatus.CALLED)
        await _make_token(db_session, org, counter, service, sequence=4)
        token = await _make_token(db_session, org, counter, service, sequence=5)

        est = await wts.estimate_for_token(db_session, token)
        assert est.people_ahead == 1  # only sequence-4 WAITING token
        assert est.estimated_minutes == 10

    async def test_historical_median_overrides_configured(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=30)
        base = datetime.now(timezone.utc) - timedelta(hours=2)
        durations = [4, 6, 8, 6, 6]  # median = 6 (>= 5 samples -> historical)
        for seq, mins in enumerate(durations, start=1):
            await _make_token(
                db_session, org, counter, service, sequence=seq,
                status=TokenStatus.COMPLETED,
                called_at=base,
                completed_at=base + timedelta(minutes=mins),
            )
        token = await _make_token(db_session, org, counter, service, sequence=10)

        est = await wts.estimate_for_token(db_session, token)
        assert est.basis == "historical"
        assert est.duration_sample_count == 5
        assert est.duration_minutes == 6
        # No waiting tokens ahead beyond the 5 completed ones -> wait is 0x6.
        assert est.people_ahead == 0
        assert est.estimated_minutes == 0
        assert est.range_min >= 0

    async def test_historical_estimate_scales_with_people_ahead(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=30)
        base = datetime.now(timezone.utc) - timedelta(hours=2)
        for seq, mins in enumerate([4, 6, 8, 6, 6], start=1):
            await _make_token(
                db_session, org, counter, service, sequence=seq,
                status=TokenStatus.COMPLETED,
                called_at=base,
                completed_at=base + timedelta(minutes=mins),
            )
        await _make_token(db_session, org, counter, service, sequence=20)
        token = await _make_token(db_session, org, counter, service, sequence=21)

        est = await wts.estimate_for_token(db_session, token)
        assert est.estimated_minutes == 6  # 1 ahead x median 6
        assert "median" in est.explanation

    async def test_thin_history_blends_with_configured(self, db_session, org):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=10)
        base = datetime.now(timezone.utc) - timedelta(hours=1)
        # 2 samples: 4 and 6 minutes -> median 5; weight = 2/(2+3) = 0.4
        for seq, mins in enumerate([4, 6], start=1):
            await _make_token(
                db_session, org, counter, service, sequence=seq,
                status=TokenStatus.COMPLETED,
                called_at=base,
                completed_at=base + timedelta(minutes=mins),
            )
        await _make_token(db_session, org, counter, service, sequence=3)  # ahead
        token = await _make_token(db_session, org, counter, service, sequence=4)

        est = await wts.estimate_for_token(db_session, token)
        assert est.basis == "blended"
        assert est.duration_sample_count == 2
        expected = round(0.4 * 5 + 0.6 * 10, 2)  # 8.0
        assert est.duration_minutes == expected
        assert est.people_ahead == 1
        assert est.estimated_minutes == 8

    async def test_timestamp_always_present_and_iso(self, db_session, org):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=10)
        token = await _make_token(db_session, org, counter, service)

        before = datetime.now(timezone.utc)
        est = await wts.estimate_for_token(db_session, token)
        after = datetime.now(timezone.utc)

        assert est.estimated_at is not None
        assert before <= est.estimated_at <= after
        payload = est.serialize()
        assert payload["estimated_at"].endswith("Z")
        # parseable ISO-8601
        datetime.fromisoformat(payload["estimated_at"].replace("Z", "+00:00"))


# -- 16.6 snapshot persistence + accuracy metrics ------------------------------

class TestAccuracyTracking:
    async def test_initial_snapshot_persisted_at_booking(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=10)
        token = await _make_token(db_session, org, counter, service)

        await wts.record_initial_snapshot(db_session, token)
        row = (await db_session.execute(
            select(WaitTimeEstimate).where(
                WaitTimeEstimate.token_id == token.id
            )
        )).scalars().first()

        assert row is not None
        assert row.snapshot_kind == "initial"
        assert row.predicted_wait_minutes == 0  # no tokens ahead
        assert row.estimation_basis == "configured"
        assert row.estimation_explanation

    async def test_insufficient_data_snapshot_persists_null_prediction(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=0)
        token = await _make_token(db_session, org, counter, service)

        await wts.record_initial_snapshot(db_session, token)
        row = (await db_session.execute(
            select(WaitTimeEstimate).where(
                WaitTimeEstimate.token_id == token.id
            )
        )).scalars().first()

        assert row is not None
        assert row.estimation_status == "insufficient_data"
        assert row.predicted_wait_minutes is None

    async def test_recalc_snapshots_recorded_for_waiting_tokens(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=10)
        ahead1 = await _make_token(db_session, org, counter, service, sequence=1)
        ahead2 = await _make_token(db_session, org, counter, service, sequence=2)

        await wts.record_recalc_for_counter(db_session, counter.id, org.id)
        rows = (await db_session.execute(
            select(WaitTimeEstimate).where(
                and_(
                    WaitTimeEstimate.snapshot_kind == "recalc",
                    # Scope to THIS test's counter: the shared in-memory DB
                    # keeps committed rows from other tests in the session.
                    WaitTimeEstimate.counter_id == counter.id,
                )
            )
        )).scalars().all()
        # Bounded snapshot fan-out: first RECALC_TOKEN_LIMIT waiting tokens.
        assert {r.token_id for r in rows} == {ahead1.id, ahead2.id}

    async def test_accuracy_mae_and_bias_computed(self, db_session, org):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=10)

        base = datetime.now(timezone.utc) - timedelta(hours=2)
        # token A: predicted 20, actual 10 -> error +10 (over-estimate)
        a = await _make_token(db_session, org, counter, service, sequence=1)
        await wts.record_initial_snapshot(db_session, a)
        row_a = (await db_session.execute(
            select(WaitTimeEstimate).where(WaitTimeEstimate.token_id == a.id)
        )).scalars().first()
        row_a.predicted_wait_minutes = 20  # scenario under test
        a.booked_at = base
        a.called_at = base + timedelta(minutes=10)
        # token B: predicted 10, actual 20 -> error -10 (under-estimate)
        b = await _make_token(db_session, org, counter, service, sequence=2)
        await wts.record_initial_snapshot(db_session, b)
        row_b = (await db_session.execute(
            select(WaitTimeEstimate).where(WaitTimeEstimate.token_id == b.id)
        )).scalars().first()
        row_b.predicted_wait_minutes = 10  # scenario under test
        b.booked_at = base
        b.called_at = base + timedelta(minutes=20)
        await db_session.flush()

        summary = await wts.accuracy_summary(db_session, org.id, days=30)
        assert summary["sample_count"] == 2
        assert summary["mae_minutes"] == 10.0
        assert summary["bias_minutes"] == 0.0  # +10 and -10 cancel
        assert summary["measured_avg_actual_wait_minutes"] == 15.0
        assert summary["estimated_avg_predicted_wait_minutes"] == 15.0

    async def test_accuracy_distinguishes_insufficient_data(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=0)
        token = await _make_token(db_session, org, counter, service)
        await wts.record_initial_snapshot(db_session, token)

        summary = await wts.accuracy_summary(db_session, org.id, days=30)
        assert summary["sample_count"] == 0
        assert summary["mae_minutes"] is None  # nothing measurable - honest
        assert summary["insufficient_data_estimates"] == 1

    async def test_accuracy_never_counts_uncalled_tokens(
        self, db_session, org
    ):
        counter = await _make_counter(db_session, org.id)
        service = await _make_service(db_session, org.id, estimated_minutes=10)
        token = await _make_token(db_session, org, counter, service)
        await wts.record_initial_snapshot(db_session, token)

        summary = await wts.accuracy_summary(db_session, org.id, days=30)
        # No called_at -> no measurable comparison yet.
        assert summary["sample_count"] == 0
        assert summary["insufficient_data_estimates"] == 0
