"""
app/routers/tokens.py
──────────────────────
Token-specific endpoints: PDF receipt download, QR code, receipt generation.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_optional_user
from app.services import token_service
from app.utils import pdf_generator
from models.counter import Counter
from models.organization import Organization
from models.service import Service
from models.token import Token
from models.user import User

router = APIRouter()


async def _assert_token_access(token: Token, user: User | None) -> None:
    """Allow access if user owns the token OR if it's a guest token."""
    if token.user_id is not None and (user is None or token.user_id != user.id):
        raise HTTPException(status_code=403, detail="Access denied")


@router.get("/{token_id}/receipt")
async def download_receipt(
    token_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate (or return cached) PDF receipt for a token (QR-001)."""
    token = await db.get(Token, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    await _assert_token_access(token, user)

    # Always produce a receipt whose QR embeds the current signed reference —
    # cached receipts from before the signed-QR upgrade carry the obsolete
    # plaintext token-id URL and tracking state (position) changes over time.
    # Generation overwrites the cached file in place (no data loss).
    org = await db.get(Organization, token.organization_id)
    counter = await db.get(Counter, token.counter_id)
    service = await db.get(Service, token.service_id) if token.service_id else None
    branch = getattr(counter, "branch", None)

    slot_label: Optional[str] = None
    if token.slot_id:
        from models.time_slot import TimeSlot
        slot = await db.get(TimeSlot, token.slot_id)
        if slot:
            slot_label = (
                f"{slot.slot_date.strftime('%d %b %Y')} · "
                f"{slot.start_time.strftime('%H:%M')}–{slot.end_time.strftime('%H:%M')}"
            )

    # Payment state (most recent payment for this token).
    payment_label: Optional[str] = None
    if token.amount_paid and token.amount_paid > 0:
        from models.payment import Payment
        result = await db.execute(
            select(Payment)
            .where(Payment.token_id == token.id)
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
        payment = result.scalars().first()
        if payment:
            payment_label = str(
                payment.status.value if hasattr(payment.status, "value") else payment.status
            )

    # Regenerate the QR each render so the receipt always embeds a signed
    # reference that the tracking page verifies independently of the PDF.
    qr_path = await qr_generator.generate_token_qr(
        token_id=token.id, org_id=token.organization_id,
        counter_id=token.counter_id, user_id=token.user_id,
    )
    token.qr_code_path = qr_path

    receipt_path = await pdf_generator.generate_token_receipt(
        token_id=token.id,
        org_name=org.name if org else "Unknown",
        token_number=token.token_number,
        counter_name=counter.counter_name if counter else "Counter",
        service_name=service.name if service else "General",
        queue_position=token.queue_position,
        booked_at=token.booked_at or token.created_at,
        qr_code_path=token.qr_code_path,
        guest_name=token.guest_name,
        branch_name=getattr(branch, "name", None) if branch else None,
        slot_label=slot_label,
        payment_label=payment_label,
        instructions=(
            f"Note your token number {token.token_number}. When it is displayed "
            "or called, proceed to the counter named on this receipt. "
            "Scan the QR to track live."
        ),
    )

    token.receipt_path = receipt_path
    await db.commit()

    return FileResponse(receipt_path, media_type="application/pdf",
                        filename=f"receipt_{token.token_number}.pdf")


@router.get("/{token_id}/qr")
async def download_qr(
    token_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the QR code image for a token (regenerated — QR-002)."""
    token = await db.get(Token, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    # Always regenerate: QR payload is a signed opaque reference whose
    # nonce/expiry must match the current signing scheme — older files
    # (pre-signed-QR scheme) are invalid for tracking.
    from app.utils import qr_generator
    qr_path = await qr_generator.generate_token_qr(
        token_id=token.id, org_id=token.organization_id,
        counter_id=token.counter_id, user_id=token.user_id
    )
    token.qr_code_path = qr_path
    await db.commit()
    return FileResponse(qr_path, media_type="image/png",
                        filename=f"qr_{token.token_number}.png")
