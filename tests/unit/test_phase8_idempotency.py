"""
tests/unit/test_phase8_idempotency.py
─────────────────────────────────────
Durable idempotency (DB-001): fingerprint stability, get-or-insert under
the UNIQUE (organization_id, key) constraint, replay of completed rows,
conflict on OPEN rows, FAILED-row reuse, and tenant scoping of keys.
"""

import pytest
from sqlalchemy import select

from app.services import idempotency_service as idem
from models.idempotency import IdempotencyKey, IdempotencyState
from tests.phase7_helpers import make_org, make_token


class TestFingerprint:
    def test_stable_across_key_order(self):
        a = idem.fingerprint_payload({"counter_id": 1, "user_id": 2})
        b = idem.fingerprint_payload({"user_id": 2, "counter_id": 1})
        assert a == b
        assert len(a) == 64

    def test_none_values_normalized(self):
        a = idem.fingerprint_payload({"counter_id": 1, "guest_name": None})
        b = idem.fingerprint_payload({"counter_id": 1})
        assert a == b

    def test_different_payload_different_fingerprint(self):
        assert idem.fingerprint_payload({"x": 1}) != idem.fingerprint_payload({"x": 2})


class TestBeginCompleteReplay:
    async def test_new_key_acquires_and_completes(self, db_session):
        org = await make_org(db_session)
        acquired, fresh = await idem.begin(
            db_session, organization_id=org.id, key="retry-1",
            operation="token.create", payload={"counter_id": 1},
        )
        assert acquired is not None
        assert fresh is True  # freshly acquired
        row = (await db_session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == "retry-1")
        )).scalar_one()
        assert row.status == IdempotencyState.OPEN

        await idem.complete(db_session, row, {"token_id": 5})
        await db_session.refresh(row)
        assert row.status == IdempotencyState.COMPLETED
        assert row.response_snapshot == {"token_id": 5}

    async def test_completed_row_replays_existing_key(self, db_session):
        org = await make_org(db_session)
        await idem.begin(db_session, organization_id=org.id, key="retry-2",
                         operation="token.create")
        row = (await db_session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == "retry-2")
        )).scalar_one()
        await idem.complete(db_session, row, {"token_id": 7})

        existing, fresh = await idem.begin(db_session, organization_id=org.id, key="retry-2",
                                           operation="token.create")
        assert existing is not None
        assert fresh is False
        assert existing.status == IdempotencyState.COMPLETED

    async def test_open_row_conflicts(self, db_session):
        org = await make_org(db_session)
        await idem.begin(db_session, organization_id=org.id, key="retry-3",
                         operation="token.create")
        existing, fresh = await idem.begin(db_session, organization_id=org.id, key="retry-3",
                                           operation="token.create")
        assert existing is not None
        assert fresh is False
        assert existing.status == IdempotencyState.OPEN

    async def test_failed_row_is_reusable(self, db_session):
        org = await make_org(db_session)
        await idem.begin(db_session, organization_id=org.id, key="retry-4",
                         operation="token.create")
        row = (await db_session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == "retry-4")
        )).scalar_one()
        await idem.fail(db_session, row)

        # fail() records the failure; a NEW attempt with the same key re-acquires
        reacquired, fresh = await idem.begin(db_session, organization_id=org.id, key="retry-4",
                                             operation="token.create")
        assert reacquired is not None
        assert fresh is True
        await db_session.refresh(row)
        assert row.status == IdempotencyState.OPEN

    async def test_double_complete_is_noop(self, db_session):
        org = await make_org(db_session)
        await idem.begin(db_session, organization_id=org.id, key="retry-5",
                         operation="token.create")
        row = (await db_session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == "retry-5")
        )).scalar_one()
        await idem.complete(db_session, row, {"token_id": 1})
        await idem.complete(db_session, row, {"token_id": 2})  # must not overwrite
        await db_session.refresh(row)
        assert row.response_snapshot == {"token_id": 1}

    async def test_empty_and_oversized_keys_rejected(self, db_session):
        org = await make_org(db_session)
        with pytest.raises(ValueError, match="empty"):
            await idem.begin(db_session, organization_id=org.id, key="   ",
                             operation="token.create")
        with pytest.raises(ValueError, match="too long"):
            await idem.begin(db_session, organization_id=org.id, key="k" * 256,
                             operation="token.create")


class TestTenantScoping:
    async def test_same_key_different_organizations_is_independent(self, db_session):
        org_a = await make_org(db_session)
        org_b = await make_org(db_session)
        first, first_fresh = await idem.begin(db_session, organization_id=org_a.id, key="shared-key",
                                              operation="token.create")
        # same key under a different organization is a separate operation —
        # cross-tenant keys never collide (tenant isolation)
        second, second_fresh = await idem.begin(db_session, organization_id=org_b.id, key="shared-key",
                                                operation="token.create")
        assert first_fresh is True
        assert second_fresh is True
        assert first.id != second.id

    async def test_replay_token_scoped_to_row_organization(self, db_session):
        org = await make_org(db_session)
        from tests.phase7_helpers import make_counter
        counter = await make_counter(db_session, org)
        token = await make_token(db_session, org, counter)
        row = IdempotencyKey(
            organization_id=org.id, key="replay-1", operation="token.create",
            status=IdempotencyState.COMPLETED,
            response_snapshot={"token_id": token.id},
        )
        db_session.add(row)
        await db_session.flush()
        replayed = await idem.replay_token(db_session, row)
        assert replayed is not None
        assert replayed.id == token.id

        # a completed row from org A cannot replay a token of another org —
        # replay_token filters on organization_id too
        org_b = await make_org(db_session)
        other = await make_token(db_session, org_b, counter)
        row_b = IdempotencyKey(
            organization_id=org_b.id, key="replay-2", operation="token.create",
            status=IdempotencyState.COMPLETED,
            response_snapshot={"token_id": token.id},  # points at ORG A's token
        )
        db_session.add(row_b)
        await db_session.flush()
        assert await idem.replay_token(db_session, row_b) is None


class TestBookingFailureStates:
    """Decision 23 close-out: the booking body records failure/success for
    an owned key so a failed attempt can never wedge the key OPEN (infinite
    409) or be mistaken for a successful booking."""

    async def _booking_env(self, db_session):
        import uuid as _uuid
        from decimal import Decimal
        from models.counter import Counter, CounterStatus
        from models.service import Service

        org = await make_org(db_session)
        counter = Counter(
            organization_id=org.id,
            counter_name=f"IDEM-{_uuid.uuid4().hex[:8]}",
            counter_number=1,
            token_prefix="E",
            daily_limit=100,
            queue_mode="day_queue",
            status=CounterStatus.ACTIVE,
        )
        db_session.add(counter)
        service = Service(
            organization_id=org.id,
            name=f"Service-{_uuid.uuid4().hex[:8]}",
            estimated_minutes=10,
            price=Decimal("0.00"),
            is_active=True,
            requires_identity_verification=False,
        )
        db_session.add(service)
        await db_session.commit()
        await db_session.refresh(counter)
        await db_session.refresh(service)
        # Decision 23 rolls back the session on booking failure, which
        # expires any captured identity objects — carry plain ids instead.
        ids = (org.id, counter.id, service.id)
        return org, counter, service, ids

    async def _token_count(self, db_session, org_id):
        from sqlalchemy import func
        from models.token import Token
        return (
            await db_session.execute(
                select(func.count()).select_from(Token).where(
                    Token.organization_id == org_id
                )
            )
        ).scalar()

    async def _get_key_row(self, db_session, org_id, key):
        return (
            await db_session.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.organization_id == org_id,
                    IdempotencyKey.key == key,
                )
            )
        ).scalar_one()

    async def test_booking_failure_records_failed_state_and_retries_apply(
        self, db_session
    ):
        """Failure → key must be durably FAILED (never stuck OPEN); the next
        attempt with the same key re-acquires and books normally."""
        from app.services.token_service import book_token

        org, counter, service, ids = await self._booking_env(db_session)
        org_id, counter_id, service_id = ids

        # attempt 1: unknown counter → ValueError AFTER the key was acquired
        with pytest.raises(ValueError, match="Counter not found"):
            await book_token(
                db_session, org_id=org_id, counter_id=999999999,
                service_id=service_id, idempotency_key="fail-retry-1",
            )
        row = await self._get_key_row(db_session, org_id, "fail-retry-1")
        assert row.status == IdempotencyState.FAILED  # durable, not stuck OPEN

        # attempt 2 (retry): the FAILED row re-acquires and the booking succeeds
        token = await book_token(
            db_session, org_id=org_id, counter_id=counter_id,
            service_id=service_id, idempotency_key="fail-retry-1",
        )
        await db_session.refresh(row)
        assert row.status == IdempotencyState.COMPLETED
        assert row.response_snapshot["token_id"] == token.id
        assert await self._token_count(db_session, org_id) == 1

        # attempt 3: the same key replays the settled token — no duplicate
        replayed = await book_token(
            db_session, org_id=org_id, counter_id=counter_id,
            service_id=service_id, idempotency_key="fail-retry-1",
        )
        assert replayed.id == token.id
        assert await self._token_count(db_session, org_id) == 1

    async def test_failed_operation_is_never_replayed_as_success(self, db_session):
        """A FAILED row carries no snapshot: replay_slot returns None and the
        begin() contract treats it as re-acquirable, not completable."""
        from app.services.token_service import book_token

        org, _counter, _service, ids = await self._booking_env(db_session)
        org_id, counter_id, service_id = ids
        with pytest.raises(ValueError, match="Counter not found"):
            await book_token(
                db_session, org_id=org_id, counter_id=999999999,
                service_id=None, idempotency_key="fail-noreplay-1",
            )
        row = await self._get_key_row(db_session, org_id, "fail-noreplay-1")
        assert row.status == IdempotencyState.FAILED
        assert row.response_snapshot is None
        assert await idem.replay_token(db_session, row) is None

    async def test_duplicate_request_conflicts_while_open(self, db_session):
        """OPEN rows (a concurrent attempt in flight) still conflict: 409
        eviction semantics of the in-flight owner are preserved."""
        from app.services.token_service import book_token

        org, counter, service, ids = await self._booking_env(db_session)
        org_id, counter_id, service_id = ids
        # simulate another request currently in flight on this key
        _, fresh = await idem.begin(
            db_session, organization_id=org_id, key="open-flight-1",
            operation="token.create", payload={"counter_id": counter_id},
        )
        assert fresh is True

        with pytest.raises(ValueError, match="idempotency conflict"):
            await book_token(
                db_session, org_id=org_id, counter_id=counter_id,
                service_id=service_id, idempotency_key="open-flight-1",
            )
        # the conflict belongs to the OTHER attempt — the key stays OPEN
        # (join path must never demote someone else's in-flight row)
        row = await self._get_key_row(db_session, org_id, "open-flight-1")
        assert row.status == IdempotencyState.OPEN
        assert await self._token_count(db_session, org_id) == 0

    async def test_client_reference_dedup_records_key_completed(self, db_session):
        """KSK-005 dedup path while a key is held: previously the early
        return left the key OPEN and the route commit froze it forever
        (every later retry 409'd). Now the settled token is recorded for
        replay and later requests replay it without creating duplicates."""
        from app.services.token_service import book_token

        org, counter, service, ids = await self._booking_env(db_session)
        org_id, counter_id, service_id = ids

        # first booking creates the token under print-reference "PRN-1"
        first = await book_token(
            db_session, org_id=org_id, counter_id=counter_id,
            service_id=service_id, client_reference="PRN-1",
            idempotency_key="dedup-key-1",
        )
        row1 = await self._get_key_row(db_session, org_id, "dedup-key-1")
        assert row1.status == IdempotencyState.COMPLETED
        assert row1.response_snapshot["token_id"] == first.id

        # retried printer reconnect: SAME client_reference, NEW key — the
        # dedup path must settle the NEW key against the same token
        dedup = await book_token(
            db_session, org_id=org_id, counter_id=counter_id,
            service_id=service_id, client_reference="PRN-1",
            idempotency_key="dedup-key-2",
        )
        assert dedup.id == first.id
        row2 = await self._get_key_row(db_session, org_id, "dedup-key-2")
        assert row2.status == IdempotencyState.COMPLETED
        assert row2.response_snapshot["token_id"] == first.id

        # a later request under dedup-key-2 replays instead of duplicating
        again = await book_token(
            db_session, org_id=org_id, counter_id=counter_id,
            service_id=service_id, idempotency_key="dedup-key-2",
        )
        assert again.id == first.id
        assert await self._token_count(db_session, org_id) == 1
