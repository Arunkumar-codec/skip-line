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
from models.payment import Payment, PaymentMethod, PaymentStatus
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

    payment.razorpay_payment_id = razorpay_payment_id
    payment.razorpay_signature = razorpay_signature
    payment.status = PaymentStatus.PAID
    payment.paid_at = datetime.now(timezone.utc)

    # Update token amount_paid
    token = await db.get(Token, payment.token_id)
    if token:
        token.amount_paid = payment.amount

    await db.commit()
    await db.refresh(payment)
    logger.info("Payment {} verified for token {}", razorpay_payment_id, payment.token_id)
    return payment


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
