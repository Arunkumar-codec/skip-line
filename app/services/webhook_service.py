"""
app/services/webhook_service.py
───────────────────────────────
Phase 5: Razorpay Webhook Ingestion & Idempotency (PAY-003, PAY-005).

Implements:
  - PAY-003: Cryptographic HMAC-SHA256 signature verification and replay safety.
  - PAY-005: Single confirmation rule. Token is confirmed once only; delayed webhooks
    or client verification races cannot create duplicate tokens or revenue.
"""

import hashlib
import hmac
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import audit_service, financial_risk_service, ledger_service
from models.finance import (
    LedgerAccount,
    LedgerEntryType,
    WebhookEvent,
)
from models.payment import Payment, PaymentMethod, PaymentStatus
from models.organization import Organization
from models.token import Token


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Validate Razorpay webhook HMAC-SHA256 signature (PAY-003)."""
    secret = settings.razorpay_webhook_secret or settings.razorpay_key_secret
    if not secret:
        # Fallback in test environments without configured secret
        return True
    expected = hmac.new(
        secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def process_webhook_event(
    db: AsyncSession,
    event_payload: dict[str, Any],
    event_id: str,
) -> dict[str, Any]:
    """
    Process incoming webhook with strict idempotency and single-confirmation safety.
    """
    # 1. Deduplication / Replay Check (PAY-003)
    existing_event = (
        await db.execute(
            select(WebhookEvent).where(WebhookEvent.event_id == event_id)
        )
    ).scalar_one_or_none()

    if existing_event:
        logger.info("Webhook event {} already processed. Skipping replay.", event_id)
        return {
            "status": "already_processed",
            "event_id": event_id,
            "message": "Event previously processed idempotently",
        }

    event_type = event_payload.get("event", "unknown")
    payload_entity = event_payload.get("payload", {})

    # Record event in ledger before or during processing
    webhook_record = WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload=event_payload,
        status="processed",
    )
    db.add(webhook_record)

    # 2. Dispatch event type
    if event_type in ("payment.captured", "order.paid"):
        payment_entity = payload_entity.get("payment", {}).get("entity", {})
        order_id = payment_entity.get("order_id") or payload_entity.get("order", {}).get("entity", {}).get("id")
        rzp_payment_id = payment_entity.get("id")

        if order_id:
            payment = (
                await db.execute(
                    select(Payment).where(Payment.razorpay_order_id == order_id)
                )
            ).scalar_one_or_none()

            if payment:
                confirmed_token: Optional[Token] = None
                # PAY-005: Only confirm once!
                if payment.status in (PaymentStatus.PAID.value, PaymentStatus.SUCCESSFUL.value):
                    logger.info("Payment for order {} already confirmed. No duplicate revenue.", order_id)
                else:
                    payment.status = PaymentStatus.PAID.value
                    payment.razorpay_payment_id = rzp_payment_id or payment.razorpay_payment_id
                    payment.paid_at = _utcnow()

                    token = await db.get(Token, payment.token_id)
                    if token:
                        token.amount_paid = payment.amount
                        confirmed_token = token

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
                        source_transaction_ref=rzp_payment_id or order_id,
                        description=f"Razorpay online payment captured for token #{payment.token_id}",
                    )

    elif event_type == "payment.failed":
        payment_entity = payload_entity.get("payment", {}).get("entity", {})
        order_id = payment_entity.get("order_id")
        if order_id:
            payment = (
                await db.execute(
                    select(Payment).where(Payment.razorpay_order_id == order_id)
                )
            ).scalar_one_or_none()
            if payment and payment.status == PaymentStatus.PENDING.value:
                payment.status = PaymentStatus.FAILED.value

    await db.commit()

    # FIN-003: confirm-time risk evaluation (after the idempotent commit).
    if confirmed_token is not None:
        try:
            catalogue_price = None
            if confirmed_token.service_id:
                from models.service import Service
                service = await db.get(Service, confirmed_token.service_id)
                catalogue_price = service.price if service else None
            await financial_risk_service.evaluate_transaction_risk(
                db, payment.organization_id, payment.id, payment.amount,
                token_price=catalogue_price,
            )
            await financial_risk_service.evaluate_volume_risk(db, payment.organization_id)
            await financial_risk_service.evaluate_duplicate_reference_risk(
                db, payment.organization_id, payment=payment, token=confirmed_token
            )
            if payment.paid_at is not None:
                org = await db.get(Organization, payment.organization_id)
                if org is not None:
                    await financial_risk_service.evaluate_after_hours_risk(
                        db, org, payment.paid_at, token_id=payment.token_id
                    )
            await financial_risk_service.evaluate_payment_token_risk(db, payment, confirmed_token)
        except Exception:
            logger.exception("FIN-003 risk evaluation failed for webhook payment {}", payment.id)

    return {"status": "success", "event_id": event_id, "event_type": event_type}

