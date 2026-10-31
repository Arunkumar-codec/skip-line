"""
app/routers/payments.py
────────────────────────
Payment endpoints: create Razorpay order, verify payment, get history.
"""

import json
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_org, get_current_user
from app.schemas.common import APIResponse
from app.schemas.finance import RefundCreateRequest, RefundRead
from app.schemas.payment import PaymentInitRequest, PaymentRead, PaymentVerifyRequest
from app.services import payment_service, refund_service, webhook_service
from models.organization import Organization
from models.token import Token
from models.user import User

router = APIRouter()


@router.post("/initiate", response_model=APIResponse)
async def initiate_payment(
    body: PaymentInitRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a Razorpay order for a pending token booking."""
    token = await db.get(Token, body.token_id)
    if not token or token.user_id != user.id:
        raise HTTPException(status_code=404, detail="Token not found")

    from models.service import Service
    service = await db.get(Service, token.service_id) if token.service_id else None
    amount = service.price if service and service.price else 0

    if amount == 0:
        raise HTTPException(status_code=400, detail="This service does not require payment")

    try:
        order = await payment_service.create_payment_order(
            db, token.id, user.id, token.organization_id, amount
        )
        return APIResponse(data=order)
    except PermissionError as e:
        # FIN-001: organization payment-method policy enforcement
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Payment initiation failed: {e}")


@router.post("/verify", response_model=APIResponse)
async def verify_payment(
    body: PaymentVerifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify Razorpay payment signature and mark as paid."""
    try:
        payment = await payment_service.verify_and_record_payment(
            db,
            razorpay_order_id=body.razorpay_order_id,
            razorpay_payment_id=body.razorpay_payment_id,
            razorpay_signature=body.razorpay_signature,
        )
        return APIResponse(message="Payment verified", data=PaymentRead.model_validate(payment))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/org/history", response_model=APIResponse)
async def org_payment_history(
    page: int = 1,
    per_page: int = 20,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Organization views its payment records."""
    import math
    payments, total = await payment_service.get_org_payment_report(db, org.id, page, per_page)
    from app.schemas.common import PaginatedResponse
    return PaginatedResponse(
        total=total, page=page, per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[PaymentRead.model_validate(p) for p in payments],
    )


# ── PAY-003..005: Razorpay Webhook Ingestion ──────────────────────────────────

@router.post("/webhook")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(None),
    x_razorpay_event_id: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Ingest Razorpay webhooks with signature verification and replay safety (PAY-003, PAY-005).
    """
    body_bytes = await request.body()
    if not webhook_service.verify_webhook_signature(body_bytes, x_razorpay_signature or ""):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON webhook payload")

    event_id = x_razorpay_event_id or payload.get("id") or f"evt_{hash(body_bytes)}"
    result = await webhook_service.process_webhook_event(db, payload, event_id)
    return APIResponse(message="Webhook processed", data=result)


# ── PAY-003..004: Payment Refunds ─────────────────────────────────────────────

@router.post("/{payment_id}/refund", response_model=APIResponse)
async def process_refund_endpoint(
    payment_id: int,
    body: RefundCreateRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """
    Organization initiates an idempotent refund for a completed payment (PAY-003, FIN-001).
    """
    try:
        refund = await refund_service.request_refund(
            db=db,
            organization_id=org.id,
            payment_id=payment_id,
            amount=body.amount,
            reason=body.reason,
            requested_by_type="organization",
            requested_by_id=org.id,
            idempotency_key=body.idempotency_key,
            auto_approve=True,
        )
        return APIResponse(message="Refund processed successfully", data=RefundRead.model_validate(refund))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
