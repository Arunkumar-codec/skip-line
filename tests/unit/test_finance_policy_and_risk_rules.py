"""
tests/unit/test_finance_policy_and_risk_rules.py
────────────────────────────────────────────────
Phase 5 closure — FIN-001 (payment-method policy + paid-booking pause)
and FIN-003 remaining risk rules (volume, duplicate references,
after-hours, waivers, price changes, payment-token mismatch).

Service-level tests use the shared in-memory test database; rows commit
mid-test (flag_risk / audit services commit), so every test builds its
own uniquely-named organization (same pattern as tests/unit/
test_ledger_and_finance.py) to stay order-independent.
"""

import hashlib
import hmac
from datetime import datetime, time, timezone
from decimal import Decimal

import pytest

from app.config import settings
from app.services import (
    financial_risk_service,
    org_service,
    payment_policy_service,
    payment_service,
    shift_service,
    token_service,
)
from app.services.schedule_service import (
    get_tenant_today,
    local_to_utc_datetime,
)
from models.admin import Admin
from models.audit_log import AuditLog
from models.branch import Branch
from models.counter import Counter
from models.counter_shift import CounterShift, CounterShiftStatus
from models.employee import Employee
from models.finance import FinancialRiskFlag
from models.organization import Organization, OrgStatus
from models.payment import Payment, PaymentMethod, PaymentStatus
from models.service import Service
from models.token import Token, TokenStatus
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule
from sqlalchemy import select


# ── Fixtures & helpers ────────────────────────────────────────────────────────

_seq = 0


def _next() -> int:
    global _seq
    _seq += 1
    return _seq


async def _make_org(db_session, **extra) -> Organization:
    n = _next()
    org = Organization(
        name=f"FIN Closure Org {n}",
        registration_number=f"REG-FINC-{n:05d}",
        org_type="hospital",
        address="1 Closure Way",
        city="Metro City",
        state="State",
        email=f"finclosure-{n}@example.com",
        mobile="9876543210",
        password_hash="x",
        status=OrgStatus.APPROVED.value,
        timezone="Asia/Kolkata",
        **extra,
    )
    db_session.add(org)
    await db_session.flush()
    return org


async def _make_counter(db_session, org) -> Counter:
    n = _next()
    counter = Counter(
        organization_id=org.id,
        counter_name=f"Closure Desk {n}",
        counter_number=n % 100 or 1,
        daily_limit=1000,
    )
    db_session.add(counter)
    await db_session.flush()
    return counter


async def _make_service(db_session, org, price: Decimal = Decimal("500.00")) -> Service:
    n = _next()
    svc = Service(
        organization_id=org.id,
        name=f"Closure Service {n}",
        price=price,
    )
    db_session.add(svc)
    await db_session.flush()
    return svc


async def _make_token(
    db_session, org, service=None, status=TokenStatus.WAITING,
    amount_paid=Decimal("500.00"), user_id=None,
) -> Token:
    counter = await _make_counter(db_session, org)
    n = _next()
    token = Token(
        organization_id=org.id,
        counter_id=counter.id,
        service_id=service.id if service else None,
        token_number=f"FINC-{n:04d}",
        sequence=n,
        status=status.value,
        amount_paid=amount_paid,
        user_id=user_id,
    )
    db_session.add(token)
    await db_session.flush()
    return token


async def _make_payment(db_session, token, org, **kwargs) -> Payment:
    payment = Payment(
        organization_id=org.id,
        token_id=token.id,
        amount=kwargs.pop("amount", token.amount_paid),
        currency=kwargs.pop("currency", "INR"),
        payment_method=kwargs.pop("payment_method", PaymentMethod.RAZORPAY.value),
        status=kwargs.pop("status", PaymentStatus.PAID.value),
        **kwargs,
    )
    db_session.add(payment)
    await db_session.flush()
    return payment


async def _make_admin(db_session) -> Admin:
    n = _next()
    admin = Admin(
        name=f"Finance Admin {n}",
        email=f"finadmin-{n}@example.com",
        password_hash="x",
        # Inactive so these fixture rows never enter the platform Super
        # Admin four-account count (see test_phase1_milestone.py).
        is_active=False,
    )
    db_session.add(admin)
    await db_session.flush()
    return admin


async def _list_flags(db, org_id, risk_type=None):
    stmt = select(FinancialRiskFlag).where(
        FinancialRiskFlag.organization_id == org_id
    )
    flags = (await db.execute(stmt)).scalars().all()
    if risk_type:
        flags = [f for f in flags if f.risk_type == risk_type]
    return list(flags)


# ══════════════════════════════════════════════════════════════════════════════
# FIN-001: Payment-method policy
# ══════════════════════════════════════════════════════════════════════════════


class TestPaymentPolicy:
    async def test_no_policy_means_unrestricted(self, db_session):
        org = await _make_org(db_session)
        policy = await payment_policy_service.get_payment_policy(db_session, org.id)
        assert policy is None
        # Every vocabulary method is permitted before any policy exists
        for method in ("razorpay", "cash", "waived", "pay_at_counter", "upi"):
            await payment_policy_service.ensure_method_allowed(db_session, org.id, method)

    async def test_set_payment_policy_validates_and_canonicalizes(self, db_session):
        org = await _make_org(db_session)
        admin = await _make_admin(db_session)

        # Unknown method is rejected
        with pytest.raises(ValueError, match="Unknown payment method"):
            await payment_policy_service.set_payment_policy(
                db_session, org.id, ["cash", "crypto"], admin
            )
        # Empty list is rejected (NULL = unrestricted; empty must never persist)
        with pytest.raises(ValueError, match="cannot be empty"):
            await payment_policy_service.set_payment_policy(db_session, org.id, [], admin)

        organization = await payment_policy_service.set_payment_policy(
            db_session, org.id, ["cash", "razorpay"], admin
        )
        assert organization.allowed_payment_methods == ["cash", "razorpay"]

        # Restore unrestricted + audit trail written for both changes
        await payment_policy_service.set_payment_policy(db_session, org.id, None, admin)
        assert organization.allowed_payment_methods is None
        logs = (await db_session.execute(
            select(AuditLog).where(AuditLog.action == "payment.policy.updated")
        )).scalars().all()
        assert len(logs) >= 2
        assert all(l.actor_type == "admin" for l in logs)

    async def test_policy_blocks_online_order_creation(self, db_session):
        """FIN-001 enforcement at `payment_service.create_payment_order` —
        out-of-policy Razorpay orders cannot be created (bypass prevention)."""
        org = await _make_org(db_session)
        admin = await _make_admin(db_session)
        service = await _make_service(db_session, org)
        token = await _make_token(db_session, org, service)
        await payment_policy_service.set_payment_policy(db_session, org.id, ["cash"], admin)

        with pytest.raises(PermissionError):
            await payment_service.create_payment_order(
                db_session, token.id, None, org.id, service.price
            )
        # No payment row leaked by the rejected initiation
        payments = (await db_session.execute(
            select(Payment).where(Payment.token_id == token.id)
        )).scalars().all()
        assert payments == []

    async def test_walkin_channel_enforces_same_policy(self, db_session):
        """FIN-001 enforcement on the assisted walk-in channel — the walk-in
        request vocabulary maps onto the same policy (online → razorpay)."""
        org = await _make_org(db_session)
        admin = await _make_admin(db_session)
        branch = Branch(
            organization_id=org.id, name=f"Branch {_next()}",
            branch_code=f"FINC-B-{_next()}", address="x", city="c",
            state="s", pincode="110001",
        )
        db_session.add(branch)
        await db_session.flush()
        employee = Employee(
            organization_id=org.id, branch_id=branch.id,
            name="Op", email=f"op-{_next()}@example.com",
            employee_code=f"FINC-E-{_next()}", password_hash="x",
        )
        db_session.add(employee)
        await db_session.flush()
        counter = await _make_counter(db_session, org)
        service = await _make_service(db_session, org)
        from app.services.schedule_service import get_tenant_today
        today = get_tenant_today(org, None)
        shift = CounterShift(
            organization_id=org.id, counter_id=counter.id, employee_id=employee.id,
            shift_date=today, status=CounterShiftStatus.ACTIVE.value,
            payment_permitted=True,
        )
        db_session.add(shift)
        await payment_policy_service.set_payment_policy(db_session, org.id, ["razorpay"], admin)
        await db_session.commit()

        # Cash counter payment is out-of-policy → rejected before any booking
        tokens_before = (await db_session.execute(
            select(Token).where(Token.organization_id == org.id)
        )).scalars().all()
        with pytest.raises(PermissionError):
            await shift_service.book_walkin_token(
                db_session, employee,
                {
                    "counter_id": counter.id,
                    "service_id": service.id,
                    "customer_name": "Walkin Guest",
                    "customer_phone": "9876500000",
                    "payment_method": "cash",
                },
            )
        tokens_after = (await db_session.execute(
            select(Token).where(Token.organization_id == org.id)
        )).scalars().all()
        assert len(tokens_after) == len(tokens_before)

        # In-policy walk-in (waived is allowed? no — policy is razorpay only)
        with pytest.raises(PermissionError):
            await shift_service.book_walkin_token(
                db_session, employee,
                {
                    "counter_id": counter.id,
                    "service_id": service.id,
                    "customer_name": "Walkin Guest",
                    "customer_phone": "9876500000",
                    "payment_method": "waived",
                    "waiver_reason": "Complementary visit",
                },
            )

        # Flip policy to permit cash + waived → both walk-ins succeed end-to-end
        await payment_policy_service.set_payment_policy(
            db_session, org.id, ["cash", "waived", "razorpay", "pay_at_counter", "upi", "free"], admin
        )
        await db_session.commit()
        token, payment = await shift_service.book_walkin_token(
            db_session, employee,
            {
                "counter_id": counter.id,
                "service_id": service.id,
                "customer_name": "Paid Guest",
                "customer_phone": "9876500001",
                "payment_method": "cash",
            },
        )
        assert token.status == TokenStatus.WAITING.value
        assert payment.status == PaymentStatus.CASH_COLLECTED.value


# ══════════════════════════════════════════════════════════════════════════════
# FIN-001: Paid-booking pause / resume
# ══════════════════════════════════════════════════════════════════════════════


class TestPaidBookingPause:
    async def test_pause_round_trip(self, db_session):
        org = await _make_org(db_session)
        service = await _make_service(db_session, org)
        token = await _make_token(db_session, org, service,
                                  status=TokenStatus.CALLED, amount_paid=Decimal("500.00"))
        admin = await _make_admin(db_session)

        paused = await payment_policy_service.pause_paid_booking(
            db_session, token, admin, "Customer requested a hold"
        )
        assert paused.payment_hold is True
        assert paused.status == TokenStatus.ON_HOLD.value
        assert paused.payment_hold_previous_status == TokenStatus.CALLED.value
        assert paused.payment_hold_reason == "Customer requested a hold"
        assert paused.payment_hold_by == admin.id
        assert paused.payment_hold_at is not None

        resumed = await payment_policy_service.resume_paid_booking(
            db_session, token, admin, "Customer returned"
        )
        assert resumed.payment_hold is False
        assert resumed.status == TokenStatus.CALLED.value
        assert resumed.payment_hold_previous_status is None
        assert resumed.payment_hold_by is None

        from app.services.audit_service import list_audit_logs  # audit trail
        actions = await list_audit_logs(db_session, limit=200)
        actions = [a for a in actions if a.target_type == "token" and a.target_id == str(token.id)]
        assert {a.action for a in actions} >= {"token.payment_hold.paused", "token.payment_hold.resumed"}

    async def test_pause_validations(self, db_session):
        org = await _make_org(db_session)
        service = await _make_service(db_session, org)
        admin = await _make_admin(db_session)

        # Free booking not pauseable
        free_token = await _make_token(db_session, org, service,
                                       amount_paid=Decimal("0.00"))
        with pytest.raises(ValueError, match="Only paid bookings"):
            await payment_policy_service.pause_paid_booking(
                db_session, free_token, admin, "Try one"
            )

        # Terminal-status booking not pauseable
        done_token = await _make_token(db_session, org, service,
                                       status=TokenStatus.COMPLETED)
        with pytest.raises(ValueError, match="waiting/called/in-progress"):
            await payment_policy_service.pause_paid_booking(
                db_session, done_token, admin, "Too late"
            )

        # Reason mandatory (>= 3 chars)
        paid_token = await _make_token(db_session, org, service)
        with pytest.raises(ValueError, match="reason"):
            await payment_policy_service.pause_paid_booking(db_session, paid_token, admin, "ab")

        # Duplicate pause rejected; resume of un-paused token rejected
        await db_session.commit()
        await payment_policy_service.pause_paid_booking(db_session, paid_token, admin, "Audit hold")
        with pytest.raises(ValueError, match="already paused"):
            await payment_policy_service.pause_paid_booking(db_session, paid_token, admin, "again")
        other = await _make_token(db_session, org, service)
        with pytest.raises(ValueError, match="not paused"):
            await payment_policy_service.resume_paid_booking(db_session, other, admin, "n/a?")

    async def test_pause_blocks_operator_actions(self, db_session):
        """Bypass prevention: while a payment hold is active, operators cannot
        call/progress/complete the token through ANY endpoint — the guard
        lives inside `token_service.perform_token_action`."""
        org = await _make_org(db_session)
        service = await _make_service(db_session, org)
        token = await _make_token(db_session, org, service)
        admin = await _make_admin(db_session)

        await payment_policy_service.pause_paid_booking(
            db_session, token, admin, "Verification in review"
        )
        await db_session.commit()

        for action in ("call", "progress", "complete"):
            with pytest.raises(PermissionError):
                await token_service.perform_token_action(
                    db_session, token.id, org.id, action
                )
        # CANCEL remains available — pause is not punitive (pause ≠ cancel);
        # perform_token_action commits the cancellation.
        cancelled = await token_service.perform_token_action(
            db_session, token.id, org.id, "cancel"
        )
        assert cancelled.status == TokenStatus.CANCELLED.value

        # Once resumed, actions work again
        resumed = await payment_policy_service.resume_paid_booking(
            db_session, token, admin, "Documents verified"
        )
        await db_session.commit()
        called = await token_service.perform_token_action(
            db_session, token.id, org.id, "call"
        )
        assert called.status == TokenStatus.CALLED.value


# ══════════════════════════════════════════════════════════════════════════════
# FIN-003: remaining risk rules
# ══════════════════════════════════════════════════════════════════════════════


class TestVolumeRisk:
    async def test_flags_at_threshold_not_below(self, db_session):
        org = await _make_org(db_session)
        token = await _make_token(db_session, org)
        for i in range(99):
            db_session.add(Payment(
                organization_id=org.id, token_id=token.id,
                amount=Decimal("10.00"), currency="INR",
            ))
        await db_session.commit()
        assert await financial_risk_service.evaluate_volume_risk(db_session, org.id) is None

        db_session.add(Payment(
            organization_id=org.id, token_id=token.id,
            amount=Decimal("10.00"), currency="INR",
        ))
        await db_session.commit()
        flag = await financial_risk_service.evaluate_volume_risk(db_session, org.id)
        assert flag is not None
        assert flag.risk_type == "unusual_volume"
        assert flag.flag_metadata["24h_count"] >= 100


class TestDuplicateReferenceRisk:
    async def test_flags_reused_client_reference(self, db_session):
        org = await _make_org(db_session)
        first = await _make_token(db_session, org)
        first.client_reference = "refc-dup-001"
        second = await _make_token(db_session, org)
        second.client_reference = "refc-dup-001"
        await db_session.commit()

        # eval on the FIRST token must see the second one
        flag = await financial_risk_service.evaluate_duplicate_reference_risk(
            db_session, org.id, token=first
        )
        assert flag is not None
        assert flag.risk_type == "duplicate_reference"

        # Unique reference → silent
        third = await _make_token(db_session, org)
        third.client_reference = "refc-unique"
        await db_session.commit()
        assert await financial_risk_service.evaluate_duplicate_reference_risk(
            db_session, org.id, token=third
        ) is None

    async def test_flags_reused_razorpay_order(self, db_session):
        org = await _make_org(db_session)
        token = await _make_token(db_session, org)
        p1 = await _make_payment(db_session, token, org,
                                 razorpay_order_id="order_dup_1")
        p2 = await _make_payment(db_session, token, org,
                                 razorpay_order_id="order_dup_1")
        await db_session.commit()
        flag = await financial_risk_service.evaluate_duplicate_reference_risk(
            db_session, org.id, payment=p2
        )
        assert flag is not None
        assert flag.flag_metadata["razorpay_order_id"] == "order_dup_1"


class TestAfterHoursRisk:
    async def _org_with_hours(self, db_session):
        org = await _make_org(db_session)
        rows = []
        for dow in range(7):
            rows.append(WeeklySchedule(
                organization_id=org.id,
                scope_level=ScheduleScopeLevel.ORGANIZATION.value,
                scope_id=org.id,
                day_of_week=dow,
                open_time=time(9, 0),
                close_time=time(17, 0),
            ))
        db_session.add_all(rows)
        await db_session.commit()
        return org

    async def test_flags_outside_hours(self, db_session):
        org = await self._org_with_hours(db_session)
        # 03:00 local on the tenant's today → outside the 09:00–17:00 window
        today = get_tenant_today(org, None)
        occurred = local_to_utc_datetime(today, time(3, 0), "Asia/Kolkata")
        flag = await financial_risk_service.evaluate_after_hours_risk(
            db_session, org, occurred
        )
        assert flag is not None
        assert flag.risk_type == "after_hours"
        assert flag.flag_metadata["day_state"] == "available"

    async def test_no_flag_inside_hours(self, db_session):
        org = await self._org_with_hours(db_session)
        today = get_tenant_today(org, None)
        occurred = local_to_utc_datetime(today, time(10, 0), "Asia/Kolkata")
        assert await financial_risk_service.evaluate_after_hours_risk(
            db_session, org, occurred
        ) is None

    async def test_skip_when_no_published_schedule(self, db_session):
        org = await _make_org(db_session)
        today = get_tenant_today(org, None)
        occurred = local_to_utc_datetime(today, time(3, 0), "Asia/Kolkata")
        assert await financial_risk_service.evaluate_after_hours_risk(
            db_session, org, occurred
        ) is None


class TestWaiverRisk:
    async def test_flags_waivers_only(self, db_session):
        org = await _make_org(db_session)
        token = await _make_token(db_session, org)

        waived = await _make_payment(
            db_session, token, org,
            payment_method=PaymentMethod.WAIVED.value,
            status=PaymentStatus.WAIVED.value,
            waiver_reason="Complementary service",
        )
        flag = await financial_risk_service.evaluate_waiver_risk(
            db_session, org.id, waived
        )
        assert flag is not None
        assert flag.risk_type == "waiver_irregularity"
        assert flag.flag_metadata["waiver_reason"] == "Complementary service"

        cash = await _make_payment(
            db_session, token, org,
            payment_method=PaymentMethod.CASH.value,
            status=PaymentStatus.CASH_COLLECTED.value,
        )
        assert await financial_risk_service.evaluate_waiver_risk(
            db_session, org.id, cash
        ) is None


class TestPriceChangeRisk:
    async def test_base_price_change_flags_affected_active_tokens(self, db_session):
        org = await _make_org(db_session)
        service = await _make_service(db_session, org, price=Decimal("500.00"))
        await _make_token(db_session, org, service, amount_paid=Decimal("500.00"))

        # Wired flow: org catalogue edit triggers evaluation automatically
        updated = await org_service.update_service(
            db_session, service.id, org.id, {"price": Decimal("700.00")}
        )
        assert updated.price == Decimal("700.00")
        flags = await _list_flags(db_session, org.id, "price_change")
        assert len(flags) == 1
        assert flags[0].flag_metadata["active_tokens_affected"] == 1

    async def test_no_flag_without_active_mismatch(self, db_session):
        org = await _make_org(db_session)
        service = await _make_service(db_session, org, price=Decimal("200.00"))
        token = await _make_token(db_session, org, service, amount_paid=Decimal("200.00"))
        token.status = TokenStatus.COMPLETED.value  # terminal → no longer active
        await db_session.commit()
        assert await financial_risk_service.evaluate_price_change_risk(
            db_session, org.id, service.id, new_total_price=Decimal("300.00")
        ) is None

    async def test_sub_service_price_change_flags(self, db_session):
        from models.sub_service import SubService
        from models.token_service_item import TokenServiceItem
        org = await _make_org(db_session)
        service = await _make_service(db_session, org, price=Decimal("100.00"))
        sub = SubService(
            service_id=service.id, name="Add-on", code=f"FINC-SS-{_next()}",
            additional_price=Decimal("50.00"),
        )
        db_session.add(sub)
        await db_session.flush()
        token = await _make_token(db_session, org, service, amount_paid=Decimal("150.00"))
        db_session.add(TokenServiceItem(
            token_id=token.id, service_id=service.id, sub_service_id=sub.id,
            price=Decimal("50.00"),
        ))
        await db_session.commit()

        flag = await financial_risk_service.evaluate_price_change_risk(
            db_session, org.id, service.id,
            new_sub_price=Decimal("60.00"), sub_service_id=sub.id,
        )
        assert flag is not None
        assert flag.flag_metadata["sub_service_id"] == sub.id


class TestPaymentTokenRisk:
    async def test_all_legs_flag(self, db_session):
        org = await _make_org(db_session)
        other_org = await _make_org(db_session)
        service = await _make_service(db_session, org)

        # (a) organization mismatch
        token = await _make_token(db_session, org, service)
        cross = await _make_payment(db_session, token, other_org)
        flag = await financial_risk_service.evaluate_payment_token_risk(
            db_session, cross, token
        )
        assert flag is not None
        assert "organization_mismatch" in flag.flag_metadata["legs"]

        # (b) confirmation attempt on a terminal token
        done = await _make_token(db_session, org, service, status=TokenStatus.CANCELLED)
        pay = await _make_payment(db_session, done, org)
        flag = await financial_risk_service.evaluate_payment_token_risk(db_session, pay, done)
        assert flag is not None
        assert "terminal_token_confirmation" in flag.flag_metadata["legs"]

        # (c) multiple successful payments on one token
        token = await _make_token(db_session, org, service)
        await _make_payment(db_session, token, org)  # already successful
        dup = await _make_payment(db_session, token, org)
        flag = await financial_risk_service.evaluate_payment_token_risk(
            db_session, dup, token
        )
        assert flag is not None
        assert "multiple_successful_payments" in flag.flag_metadata["legs"]

        # (d) amount deviates from the booked price snapshot
        token = await _make_token(db_session, org, service, amount_paid=Decimal("500.00"))
        drift = await _make_payment(db_session, token, org, amount=Decimal("800.00"))
        flag = await financial_risk_service.evaluate_payment_token_risk(
            db_session, drift, token
        )
        assert flag is not None
        assert "amount_snapshot_mismatch" in flag.flag_metadata["legs"]

    async def test_clean_case_passes(self, db_session):
        org = await _make_org(db_session)
        service = await _make_service(db_session, org)
        token = await _make_token(db_session, org, service, amount_paid=Decimal("500.00"))
        pay = await _make_payment(db_session, token, org,
                                  amount=Decimal("500.00"),
                                  status=PaymentStatus.PAID.value)
        assert await financial_risk_service.evaluate_payment_token_risk(
            db_session, pay, token
        ) is None


class TestVerifyWiredRiskEvaluation:
    async def test_verify_flags_duplicate_successful_payment(self, db_session):
        """Wired flow: `/payments/verify` confirmation runs FIN-003 evaluation
        — a second successful payment on the same token surfaces a flag while
        the confirmation itself still succeeds exactly once."""
        org = await _make_org(db_session)
        service = await _make_service(db_session, org)
        token = await _make_token(db_session, org, service, amount_paid=Decimal("500.00"))
        await _make_payment(db_session, token, org, status=PaymentStatus.PAID.value)

        pending = Payment(
            organization_id=org.id, token_id=token.id,
            amount=Decimal("500.00"), currency="INR",
            payment_method=PaymentMethod.RAZORPAY.value,
            status=PaymentStatus.PENDING.value,
            razorpay_order_id="order_wiretest_1",
        )
        db_session.add(pending)
        await db_session.commit()

        message = f"order_wiretest_1|pay_wiretest_1"
        signature = hmac.new(
            settings.razorpay_key_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        confirmed = await payment_service.verify_and_record_payment(
            db_session, "order_wiretest_1", "pay_wiretest_1", signature
        )
        assert confirmed.status == PaymentStatus.PAID.value
        flags = await _list_flags(db_session, org.id, "payment_token_mismatch")
        assert any("multiple_successful_payments" in (f.flag_metadata or {}).get("legs", [])
                   for f in flags)
