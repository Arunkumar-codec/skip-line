"""
app/services/payment_service.py
────────────────────────────────
Razorpay payment integration: create order, verify signature, record payment.

Fix applied (ISSUE-06):
  - get_org_payment_report: added selectinload(Payment.token) so that
    org/payments.html can access p.token.token_number without triggering
    async lazy-load errors.
"""

import hashlib
import hmac
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import razorpay
from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.services import financial_risk_service, ledger_service
from models.finance import LedgerAccount, LedgerEntryType
from models.payment import Payment, PaymentMethod, PaymentStatus
from models.time_slot import TimeSlot
from models.token import Token


def _razorpay_client() -> razorpay.Client:
    return razorpay.Client(
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
    )


async def create_payment_order(
    db: AsyncSession, token_id: int, user_id: Optional[int], org_id: int, amount: Decimal
) -> dict:
    """
    Create a Razorpay order and persist a pending Payment record.
    Returns the Razorpay order data needed by the frontend checkout.
    """
    client = _razorpay_client()
    # FIN-001: organization payment-method policy enforcement. Runs before
    # any payment/order side effect so an out-of-policy online payment can
    # never be created through this channel.
    from app.services import payment_policy_service
    await payment_policy_service.ensure_method_allowed(db, org_id, PaymentMethod.RAZORPAY.value)
    amount_paise = int(amount * 100)   # Razorpay uses smallest currency unit

    order = client.order.create({
        "amount": amount_paise,
        "currency": settings.razorpay_currency,
        "receipt": f"token_{token_id}",
        "payment_capture": 1,
    })

    payment = Payment(
        token_id=token_id,
        user_id=user_id,
        organization_id=org_id,
        amount=amount,
        currency=settings.razorpay_currency,
        payment_method=PaymentMethod.RAZORPAY,
        status=PaymentStatus.PENDING,
        razorpay_order_id=order["id"],
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    return {
        "order_id": order["id"],
        "amount": amount_paise,
        "currency": settings.razorpay_currency,
        "key_id": settings.razorpay_key_id,
        "payment_id": payment.id,
    }


def _verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Validate Razorpay HMAC-SHA256 signature."""
    message = f"{order_id}|{payment_id}"
    expected = hmac.new(
        settings.razorpay_key_secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def verify_and_record_payment(
    db: AsyncSession,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> Payment:
    """Verify Razorpay signature and mark payment as paid."""
    if not _verify_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
        raise ValueError("Invalid payment signature")

    result = await db.execute(
        select(Payment).where(Payment.razorpay_order_id == razorpay_order_id)
    )
    payment = result.scalar_one_or_none()
    if not payment:
        raise ValueError("Payment record not found")

    # PAY-005: Idempotent return if already confirmed
    if payment.status in (PaymentStatus.PAID.value, PaymentStatus.PAID, PaymentStatus.SUCCESSFUL.value):
        return payment

    payment.razorpay_payment_id = razorpay_payment_id
    payment.razorpay_signature = razorpay_signature
    payment.status = PaymentStatus.PAID
    payment.paid_at = datetime.now(timezone.utc)

    # Update token amount_paid
    token = await db.get(Token, payment.token_id)
    if token:
        token.amount_paid = payment.amount

    # Post Double-Entry Ledger Entry (LED-001..004)
    await ledger_service.post_double_entry(
        db=db,
        organization_id=payment.organization_id,
        debit_account=LedgerAccount.GATEWAY,
        credit_account=LedgerAccount.REVENUE,
        amount=payment.amount,
        currency=payment.currency,
        entry_type=LedgerEntryType.CHARGE,
        token_id=payment.token_id,
        payment_id=payment.id,
        source_transaction_ref=razorpay_payment_id,
        description=f"Online payment verified for token #{payment.token_id}",
    )

    await db.commit()
    await db.refresh(payment)
    logger.info("Payment {} verified for token {}", razorpay_payment_id, payment.token_id)

    # FIN-003: confirm-time risk evaluation. Confirmation already committed;
    # risk flagging failures must never roll back a legit payment.
    from models.organization import Organization
    try:
        org = await db.get(Organization, payment.organization_id)
        token_obj = await db.get(Token, payment.token_id)
        catalogue_price = None
        if token_obj is not None and token_obj.service_id:
            from models.service import Service
            service = await db.get(Service, token_obj.service_id)
            catalogue_price = service.price if service else None
        await financial_risk_service.evaluate_transaction_risk(
            db, payment.organization_id, payment.id, payment.amount,
            token_price=catalogue_price,
        )
        await financial_risk_service.evaluate_volume_risk(db, payment.organization_id)
        await financial_risk_service.evaluate_duplicate_reference_risk(
            db, payment.organization_id, payment=payment, token=token_obj
        )
        if org is not None and payment.paid_at is not None:
            await financial_risk_service.evaluate_after_hours_risk(
                db, org, payment.paid_at, token_id=payment.token_id
            )
        if token_obj is not None:
            await financial_risk_service.evaluate_payment_token_risk(db, payment, token_obj)
    except Exception:
        logger.exception("FIN-003 risk evaluation failed for payment {}", payment.id)
    return payment


async def release_expired_payment_holds(db: AsyncSession) -> int:
    """
    Sweeper fulfilling Phase 3 Decision 14 & SCH-006:
    Release capacity holds on time slots where held_count > 0 and no unconfirmed payment active.
    Returns the count of released hold units.
    """
    stmt = select(TimeSlot).where(TimeSlot.held_count > 0)
    slots = (await db.execute(stmt)).scalars().all()
    released = 0
    for slot in slots:
        # Check active pending payments for this slot
        pending_count = (await db.execute(
            select(func.count(Payment.id))
            .join(Token, Payment.token_id == Token.id)
            .where(
                Token.slot_id == slot.id,
                Payment.status == PaymentStatus.PENDING.value,
            )
        )).scalar() or 0

        excess_holds = slot.held_count - pending_count
        if excess_holds > 0:
            slot.held_count = max(0, slot.held_count - excess_holds)
            released += excess_holds

    if released > 0:
        await db.commit()
    return released


async def get_org_payment_report(
    db: AsyncSession, org_id: int, page: int = 1, per_page: int = 20
) -> tuple[list[Payment], int]:
    """
    FIX (ISSUE-06): Added selectinload(Payment.token) so org/payments.html
    can access p.token.token_number without triggering async lazy-load errors.
    Also returns (payments, total_count) — callers must separately compute
    total_revenue; see views_org.py payments_page() for updated context.
    """
    stmt = select(Payment).where(Payment.organization_id == org_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
    stmt = (
        stmt
        .options(selectinload(Payment.token))
        .order_by(Payment.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    payments = (await db.execute(stmt)).scalars().all()
    return list(payments), total


async def get_org_total_revenue(db: AsyncSession, org_id: int) -> float:
    """Return total paid revenue for an organization (used by payments page)."""
    result = await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.organization_id == org_id, Payment.status == PaymentStatus.PAID)
    )
    return float(result.scalar() or 0)
