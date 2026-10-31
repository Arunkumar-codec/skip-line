"""
tests/unit/test_ledger_and_finance.py
─────────────────────────────────────
Unit tests for Phase 5 Finance & Ledgering.

Covers:
  - LED-001..004: Double-entry immutable ledger & institutional totals.
  - CASH-001..003: Cash drawer sessions, denomination calculation & breakdown reports.
  - PAY-003..005: Idempotent refunds, webhooks & single-confirmation integrity.
  - FIN-001..004: Admin payout holds/releases, 72h bank security holds & risk rules.
  - SCH-006 / Decision 14: Expired payment hold release sweeper.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.services import (
    bank_account_service,
    cash_service,
    financial_risk_service,
    ledger_service,
    payment_service,
    payout_service,
    refund_service,
    webhook_service,
)
from models.counter import Counter
from models.counter_shift import CounterShift, CounterShiftStatus
from models.finance import (
    BankAccountStatus,
    CashSession,
    CashSessionStatus,
    FinancialRiskFlag,
    LedgerAccount,
    LedgerDirection,
    LedgerEntry,
    LedgerEntryType,
    Payout,
    PayoutStatus,
    Refund,
    RefundStatus,
)
from models.organization import Organization, OrgStatus
from models.payment import Payment, PaymentMethod, PaymentStatus
from models.service import Service
from models.time_slot import TimeSlot
from models.token import Token, TokenStatus


# ── Fixtures & Helpers ────────────────────────────────────────────────────────

# These tests commit mid-test, so committed rows persist in the shared
# session-scoped database across tests (conftest's teardown rollback cannot
# undo them). The org therefore needs a unique email/registration each run,
# following the same pattern as tests/unit/test_slot_booking.py.
_org_seq = 0

@pytest.fixture
async def fin_org(db_session):
    global _org_seq
    _org_seq += 1
    name = f"Finance Test Org {_org_seq}"
    org = Organization(
        name=name,
        registration_number=f"REG-FIN-{_org_seq:05d}",
        org_type="hospital",
        address="Test Address",
        city="Test City",
        state="Test State",
        email=f"fin-{_org_seq}@fin.example.com",
        mobile="9876543210",
        password_hash="hash",
        status=OrgStatus.APPROVED.value,
        timezone="Asia/Kolkata",
    )
    db_session.add(org)
    await db_session.flush()
    return org


@pytest.fixture
async def fin_counter(db_session, fin_org):
    counter = Counter(
        organization_id=fin_org.id,
        counter_name="Desk 1",
        counter_number=1,
        daily_limit=100,
    )
    db_session.add(counter)
    await db_session.flush()
    return counter


@pytest.fixture
async def fin_service(db_session, fin_org):
    svc = Service(
        organization_id=fin_org.id,
        name="Premium Registration",
        price=Decimal("500.00"),
    )
    db_session.add(svc)
    await db_session.flush()
    return svc


@pytest.fixture
async def fin_token(db_session, fin_org, fin_counter, fin_service):
    token = Token(
        organization_id=fin_org.id,
        counter_id=fin_counter.id,
        service_id=fin_service.id,
        token_number="FIN-001",
        sequence=1,
        status=TokenStatus.WAITING.value,
        amount_paid=Decimal("500.00"),
    )
    db_session.add(token)
    await db_session.flush()
    return token


# ── LED-001..004: Double-Entry Immutable Ledger ───────────────────────────────

class TestDoubleEntryLedger:
    async def test_post_double_entry_balanced(self, db_session, fin_org, fin_token):
        debit, credit = await ledger_service.post_double_entry(
            db=db_session,
            organization_id=fin_org.id,
            debit_account=LedgerAccount.GATEWAY,
            credit_account=LedgerAccount.REVENUE,
            amount=Decimal("500.00"),
            currency="INR",
            entry_type=LedgerEntryType.CHARGE,
            token_id=fin_token.id,
            source_transaction_ref="pay_12345",
            description="Test payment charge",
        )
        await db_session.commit()

        assert debit.id is not None
        assert credit.id is not None
        assert debit.direction == LedgerDirection.DEBIT.value
        assert credit.direction == LedgerDirection.CREDIT.value
        assert debit.amount == Decimal("500.00")
        assert credit.amount == Decimal("500.00")
        assert debit.account == LedgerAccount.GATEWAY.value
        assert credit.account == LedgerAccount.REVENUE.value

    async def test_post_double_entry_rejects_negative_or_zero(self, db_session, fin_org):
        with pytest.raises(ValueError, match="strictly positive"):
            await ledger_service.post_double_entry(
                db=db_session,
                organization_id=fin_org.id,
                debit_account=LedgerAccount.GATEWAY,
                credit_account=LedgerAccount.REVENUE,
                amount=Decimal("0.00"),
            )

    async def test_post_adjustment_entry(self, db_session, fin_org):
        deb, crd = await ledger_service.post_adjustment(
            db=db_session,
            organization_id=fin_org.id,
            account=LedgerAccount.CASH,
            direction=LedgerDirection.CREDIT,
            amount=Decimal("150.00"),
            reason="Corrected counted cash shortage",
            actor_type="organization",
            actor_id=fin_org.id,
        )
        await db_session.commit()

        assert deb.amount == Decimal("150.00")
        assert crd.account == LedgerAccount.CASH.value
        assert "Corrected" in (deb.description or "")

    async def test_ledger_totals_derivation(self, db_session, fin_org):
        # 1. Post Online Charge: Debit Gateway ₹1000, Credit Revenue ₹1000
        await ledger_service.post_double_entry(
            db=db_session,
            organization_id=fin_org.id,
            debit_account=LedgerAccount.GATEWAY,
            credit_account=LedgerAccount.REVENUE,
            amount=Decimal("1000.00"),
        )
        # 2. Post Cash Charge: Debit Cash ₹500, Credit Revenue ₹500
        await ledger_service.post_double_entry(
            db=db_session,
            organization_id=fin_org.id,
            debit_account=LedgerAccount.CASH,
            credit_account=LedgerAccount.REVENUE,
            amount=Decimal("500.00"),
        )
        # 3. Post Refund: Debit Refunds ₹200, Credit Gateway ₹200
        await ledger_service.post_double_entry(
            db=db_session,
            organization_id=fin_org.id,
            debit_account=LedgerAccount.REFUNDS,
            credit_account=LedgerAccount.GATEWAY,
            amount=Decimal("200.00"),
            entry_type=LedgerEntryType.REFUND,
        )
        await db_session.commit()

        totals = await ledger_service.get_ledger_totals(db_session, fin_org.id)
        assert totals["gross_revenue"] == Decimal("1500.00")
        assert totals["cash_collected"] == Decimal("500.00")
        assert totals["gateway_collected"] == Decimal("1000.00")
        assert totals["refunds"] == Decimal("200.00")
        assert totals["net_revenue"] == Decimal("1300.00")


# ── PAY-003..005: Refunds & Idempotency ───────────────────────────────────────

class TestRefunds:
    async def test_idempotent_refund_lifecycle(self, db_session, fin_org, fin_token):
        payment = Payment(
            organization_id=fin_org.id,
            token_id=fin_token.id,
            amount=Decimal("500.00"),
            refund_amount=Decimal("0.00"),
            currency="INR",
            payment_method=PaymentMethod.RAZORPAY.value,
            status=PaymentStatus.PAID.value,
            razorpay_payment_id="pay_ref_test_01",
        )
        db_session.add(payment)
        await db_session.commit()

        # First refund: partial ₹200 with idempotency key
        key = "idemp_rfnd_001"
        rf1 = await refund_service.request_refund(
            db=db_session,
            organization_id=fin_org.id,
            payment_id=payment.id,
            amount=Decimal("200.00"),
            reason="Customer cancelled",
            requested_by_type="organization",
            requested_by_id=fin_org.id,
            idempotency_key=key,
            auto_approve=True,
        )

        assert rf1.status == RefundStatus.PROCESSED.value
        assert rf1.amount == Decimal("200.00")
        assert payment.refund_amount == Decimal("200.00")
        assert payment.status == PaymentStatus.PARTIALLY_REFUNDED.value

        # Replay with same key -> returns identical refund without double debit
        rf1_replay = await refund_service.request_refund(
            db=db_session,
            organization_id=fin_org.id,
            payment_id=payment.id,
            amount=Decimal("200.00"),
            reason="Customer cancelled",
            requested_by_type="organization",
            requested_by_id=fin_org.id,
            idempotency_key=key,
            auto_approve=True,
        )
        assert rf1_replay.id == rf1.id
        assert payment.refund_amount == Decimal("200.00")

        # Over-balance request rejected while balance remains — checked before
        # the full refund because refund_service's status guard fires first on
        # a fully-refunded payment and would mask the balance-guard message.
        with pytest.raises(ValueError, match="exceeds remaining refundable balance"):
            await refund_service.request_refund(
                db=db_session,
                organization_id=fin_org.id,
                payment_id=payment.id,
                amount=Decimal("600.00"),
                reason="Over-refund",
                requested_by_type="organization",
                requested_by_id=fin_org.id,
            )

        # Second refund: full remaining balance (₹300)
        rf2 = await refund_service.request_refund(
            db=db_session,
            organization_id=fin_org.id,
            payment_id=payment.id,
            amount=Decimal("300.00"),
            reason="Remaining balance refund",
            requested_by_type="organization",
            requested_by_id=fin_org.id,
            idempotency_key="idemp_rfnd_002",
            auto_approve=True,
        )
        assert payment.refund_amount == Decimal("500.00")
        assert payment.status == PaymentStatus.REFUNDED.value


# ── CASH-001..003: Cash Sessions & Reports ───────────────────────────────────

class TestCashManagement:
    def test_denominations_calculator(self):
        denoms = {"500": 3, "200": 4, "100": 5, "50": 2, "10": 10}
        # 1500 + 800 + 500 + 100 + 100 = 3000
        total = cash_service.calculate_denominations_total(denoms)
        assert total == Decimal("3000.00")

    async def test_cash_session_discrepancy_states(self, db_session, fin_org, fin_counter):
        # 1. MATCHED: starting 100, collected 400, counted 500
        session1 = await cash_service.open_cash_session(
            db=db_session,
            organization_id=fin_org.id,
            counter_id=fin_counter.id,
            employee_id=1,
            starting_float=Decimal("100.00"),
        )
        session1.collected_cash = Decimal("400.00")
        await db_session.commit()

        closed1 = await cash_service.close_cash_session(
            db=db_session,
            session_id=session1.id,
            counted_cash=Decimal("500.00"),
        )
        assert closed1.status == CashSessionStatus.MATCHED.value
        assert closed1.discrepancy == Decimal("0.00")

        # 2. SHORTAGE: starting 100, collected 400, counted 450
        session2 = await cash_service.open_cash_session(
            db=db_session,
            organization_id=fin_org.id,
            counter_id=fin_counter.id,
            employee_id=1,
            starting_float=Decimal("100.00"),
        )
        session2.collected_cash = Decimal("400.00")
        await db_session.commit()

        closed2 = await cash_service.close_cash_session(
            db=db_session,
            session_id=session2.id,
            counted_cash=Decimal("450.00"),
        )
        assert closed2.status == CashSessionStatus.SHORTAGE.value
        assert closed2.discrepancy == Decimal("-50.00")

        # 3. EXCESS: starting 100, collected 400, counted 520
        session3 = await cash_service.open_cash_session(
            db=db_session,
            organization_id=fin_org.id,
            counter_id=fin_counter.id,
            employee_id=1,
            starting_float=Decimal("100.00"),
        )
        session3.collected_cash = Decimal("400.00")
        await db_session.commit()

        closed3 = await cash_service.close_cash_session(
            db=db_session,
            session_id=session3.id,
            counted_cash=Decimal("520.00"),
        )
        assert closed3.status == CashSessionStatus.EXCESS.value
        assert closed3.discrepancy == Decimal("20.00")


# ── FIN-001..004: Payouts, Bank Holds & Risk Flags ────────────────────────────

class TestAdminFinancialControls:
    async def test_payout_hold_and_release(self, db_session, fin_org):
        payout = await payout_service.schedule_payout(
            db=db_session,
            organization_id=fin_org.id,
            amount=Decimal("5000.00"),
        )
        assert payout.status == PayoutStatus.SCHEDULED.value

        # Place hold
        held = await payout_service.hold_payout(
            db=db_session,
            payout_id=payout.id,
            reason="KYC document review pending",
            held_by_type="admin",
            held_by_id=1,
        )
        assert held.status == PayoutStatus.HELD.value
        assert held.hold_reason == "KYC document review pending"

        # Release hold
        released = await payout_service.release_payout(
            db=db_session,
            payout_id=payout.id,
            released_by_type="admin",
            released_by_id=1,
        )
        assert released.status == PayoutStatus.SCHEDULED.value
        assert released.hold_reason is None

        # Disburse
        paid = await payout_service.disburse_payout(
            db=db_session,
            payout_id=payout.id,
        )
        assert paid.status == PayoutStatus.PAID.value

    async def test_bank_account_72h_security_hold(self, db_session, fin_org):
        account = await bank_account_service.update_bank_account(
            db=db_session,
            organization_id=fin_org.id,
            account_number="123456789012",
            ifsc_code="HDFC0001234",
            account_holder_name="Finance Head",
            bank_name="HDFC Bank",
        )
        assert account.status == BankAccountStatus.PENDING_VERIFICATION.value
        assert account.held_until is not None
        # Check hold duration ~ 72 hours. SQLite (and MySQL DATETIME) round-trip
        # datetimes as offset-naive values, so normalize to UTC before comparing.
        held_until = account.held_until
        if held_until.tzinfo is None:
            held_until = held_until.replace(tzinfo=timezone.utc)
        diff = held_until - datetime.now(timezone.utc)
        assert 71 <= diff.total_seconds() / 3600 <= 73

        # Verify lifts hold
        verified = await bank_account_service.verify_bank_account(
            db=db_session,
            organization_id=fin_org.id,
        )
        assert verified.status == BankAccountStatus.VERIFIED.value
        assert verified.held_until is None

    async def test_proportional_risk_evaluation(self, db_session, fin_org):
        # 1. High value transaction flag
        flags = await financial_risk_service.evaluate_transaction_risk(
            db=db_session,
            organization_id=fin_org.id,
            payment_id=99,
            amount=Decimal("25000.00"),
        )
        assert len(flags) == 1
        assert flags[0].risk_type == "unusual_amount"

        # 2. Price mismatch flag
        mismatch_flags = await financial_risk_service.evaluate_transaction_risk(
            db=db_session,
            organization_id=fin_org.id,
            payment_id=100,
            amount=Decimal("300.00"),
            token_price=Decimal("500.00"),
        )
        assert any(f.risk_type == "payment_mismatch" for f in mismatch_flags)

        # 3. Resolve flag
        resolved = await financial_risk_service.resolve_risk_flag(
            db=db_session,
            flag_id=flags[0].id,
            resolved_by="Super Admin 1",
            resolution_notes="Authorized bulk purchase verified with org",
        )
        assert resolved.status == "resolved"


# ── Webhook Idempotency & Expired Holds Sweeper ───────────────────────────────

class TestWebhookAndSweeper:
    async def test_webhook_single_confirmation_rule(self, db_session, fin_org, fin_token):
        payment = Payment(
            organization_id=fin_org.id,
            token_id=fin_token.id,
            amount=Decimal("500.00"),
            currency="INR",
            payment_method=PaymentMethod.RAZORPAY.value,
            status=PaymentStatus.PENDING.value,
            razorpay_order_id="order_webhook_001",
        )
        db_session.add(payment)
        await db_session.commit()

        payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_wh_001",
                        "order_id": "order_webhook_001",
                        "amount": 50000,
                    }
                }
            },
        }

        # First webhook arrival
        res1 = await webhook_service.process_webhook_event(
            db=db_session,
            event_payload=payload,
            event_id="evt_001",
        )
        assert res1["status"] == "success"
        assert payment.status == PaymentStatus.PAID.value

        # Replayed webhook arrival -> safe idempotency acknowledgment
        res2 = await webhook_service.process_webhook_event(
            db=db_session,
            event_payload=payload,
            event_id="evt_001",
        )
        assert res2["status"] == "already_processed"

    async def test_release_expired_payment_holds_sweeper(self, db_session, fin_org, fin_counter):
        slot = TimeSlot(
            organization_id=fin_org.id,
            counter_id=fin_counter.id,
            slot_date=date(2026, 11, 15),
            start_time=datetime.now().time(),
            end_time=(datetime.now() + timedelta(minutes=30)).time(),
            capacity=10,
            held_count=3,
        )
        db_session.add(slot)
        await db_session.commit()

        # Sweeper runs: with 0 active pending payments, all 3 held slots are released
        released = await payment_service.release_expired_payment_holds(db_session)
        assert released == 3
        assert slot.held_count == 0

