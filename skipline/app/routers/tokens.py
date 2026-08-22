"""
app/routers/tokens.py
──────────────────────
Token-specific endpoints: PDF receipt download, QR code, receipt generation.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
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
    """Generate (or return cached) PDF receipt for a token."""
    token = await db.get(Token, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    await _assert_token_access(token, user)

    # Return cached receipt if it exists
    if token.receipt_path and Path(token.receipt_path).exists():
        return FileResponse(token.receipt_path, media_type="application/pdf",
                            filename=f"receipt_{token.token_number}.pdf")

    # Generate receipt
    org = await db.get(Organization, token.organization_id)
    counter = await db.get(Counter, token.counter_id)
    service = await db.get(Service, token.service_id) if token.service_id else None

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
    """Return the QR code image for a token."""
    token = await db.get(Token, token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    if token.qr_code_path and Path(token.qr_code_path).exists():
        return FileResponse(token.qr_code_path, media_type="image/png",
                            filename=f"qr_{token.token_number}.png")

    # Regenerate if missing
    from app.utils import qr_generator
    qr_path = await qr_generator.generate_token_qr(
        token_id=token.id, org_id=token.organization_id,
        counter_id=token.counter_id, user_id=token.user_id
    )
    token.qr_code_path = qr_path
    await db.commit()
    return FileResponse(qr_path, media_type="image/png",
                        filename=f"qr_{token.token_number}.png")
