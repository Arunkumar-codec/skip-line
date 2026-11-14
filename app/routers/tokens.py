"""
app/routers/tokens.py
──────────────────────
Token-specific endpoints: PDF receipt download, QR code, receipt generation.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_optional_user
from app.services import token_service
from app.utils import pdf_generator, qr_generator, signed_qr
from models.branch import Branch
from models.counter import Counter
from models.organization import Organization
from models.service import Service
from models.token import Token, TokenStatus
from models.user import User

router = APIRouter()


# Uniform denial for receipt/QR downloads: a nonexistent token and a token
# the caller may not access return the SAME 404 response, so probing an
# enumerated numeric id never reveals whether an id exists (QR-002 tracking
# precedent — "renders the same uninformative error so no token can be probed").
_DENY = HTTPException(status_code=404, detail="Token not found")


async def _assert_token_access(
    token: Token, user: User | None, ref: Optional[str] = None
) -> None:
    """
    Receipt/QR download authorization — HARDENED (HIGH finding, Nov 4 2026).

    The numeric token id alone NEVER authorizes a guest download:

    - User-owned tokens (`user_id` set): only the owning user's session
      (cookie or Bearer) may download. Anyone else is denied — including
      unauthenticated callers and sessions of other roles (org/employee/
      admin tokens resolve to `user=None` here and are refused).
    - Guest tokens (`user_id is None`): caller must present a valid,
      purpose-bound, HMAC-signed receipt reference (`?ref=…`) issued with
      the booking (`token_service.guest_receipt_ref` →
      `signed_qr PURPOSE_RECEIPT`). Verification is constant-time
      (`hmac.compare_digest` in signed_qr) and binds token id + org id;
      missing/expired/tampered/mismatched refs are all denied identically.

    Foreign-purpose refs (scan/track) can never satisfy the receipt purpose
    because each purpose derives its own HMAC subkey, so a track ref
    presented here fails signature verification.
    """
    if token.user_id is not None:
        if user is None or user.id != token.user_id:
            raise _DENY
        return

    status, payload = signed_qr.verify_token_ref(
        ref or "", signed_qr.PURPOSE_RECEIPT
    )
    if status != signed_qr.STATUS_VALID:
        raise _DENY
    if payload.token_id != token.id or payload.org_id != token.organization_id:
        raise _DENY


@router.get("/{token_id}/receipt")
async def download_receipt(
    token_id: int,
    ref: Optional[str] = Query(default=None, alias="ref",
                               description=("Signed receipt reference "
                                            "(required for guest tokens)")),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate (or return cached) PDF receipt for a token (QR-001)."""
    token = await db.get(Token, token_id)
    if not token:
        raise _DENY
    await _assert_token_access(token, user, ref)

    # Always produce a receipt whose QR embeds the current signed reference —
    # cached receipts from before the signed-QR upgrade carry the obsolete
    # plaintext token-id URL and tracking state (position) changes over time.
    # Generation overwrites the cached file in place (no data loss).
    org = await db.get(Organization, token.organization_id)
    counter = await db.get(Counter, token.counter_id)
    service = await db.get(Service, token.service_id) if token.service_id else None
    # WP1 acceptance fix (2026-11-10): `Counter.branch` is a default
    # lazy="select" relationship — accessing it synchronously here raised
    # sqlalchemy.exc.MissingGreenlet inside the async route (unhandled 500 →
    # receipt downloads broken for any counter that has a branch). Load the
    # branch explicitly instead.
    branch: Optional[Branch] = None
    if counter is not None and counter.branch_id:
        branch = await db.get(Branch, counter.branch_id)

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

    # Guest-PII protection: receipts downloaded via a guest receipt ref
    # (i.e., by anyone holding the ref, not necessarily the guest that
    # booked) MUST NOT carry the guest's name — MASTER treats receipts as
    # org/token/counter/date/position/QR content; a guest name on a
    # ref-bearing document would leak personal data to whoever presents
    # the ref. Owner downloads of user-owned tokens are unaffected.
    receipt_guest_name = token.guest_name if token.user_id is not None else None

    # Phase 10 (§16.5/§22): the printed estimate is a STATIC snapshot of the
    # authoritative estimator's output at generation time, always stamped
    # with its generation moment. Insufficient data prints honestly.
    estimate_snapshot: dict = {}
    if token.status == TokenStatus.WAITING:
        from app.services import wait_time_service
        est = await wait_time_service.estimate_for_token(db, token)
        estimate_snapshot = {
            "estimated_wait_minutes": est.estimated_minutes,
            "estimated_wait_range_min": est.range_min,
            "estimated_wait_range_max": est.range_max,
            "estimate_status": est.status,
            "estimate_generated_at": est.estimated_at.replace(tzinfo=None)
            if est.estimated_at.tzinfo else est.estimated_at,
        }

    receipt_path = await pdf_generator.generate_token_receipt(
        token_id=token.id,
        org_name=org.name if org else "Unknown",
        token_number=token.token_number,
        counter_name=counter.counter_name if counter else "Counter",
        service_name=service.name if service else "General",
        queue_position=token.queue_position,
        booked_at=token.booked_at or token.created_at,
        qr_code_path=token.qr_code_path,
        guest_name=receipt_guest_name,
        branch_name=getattr(branch, "name", None) if branch else None,
        slot_label=slot_label,
        payment_label=payment_label,
        estimated_wait_minutes=estimate_snapshot.get("estimated_wait_minutes"),
        estimated_wait_range_min=estimate_snapshot.get("estimated_wait_range_min"),
        estimated_wait_range_max=estimate_snapshot.get("estimated_wait_range_max"),
        estimate_status=estimate_snapshot.get("estimate_status"),
        estimate_generated_at=estimate_snapshot.get("estimate_generated_at"),
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
    ref: Optional[str] = Query(default=None, alias="ref",
                               description=("Signed receipt reference "
                                            "(required for guest tokens)")),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the QR code image for a token (regenerated — QR-002)."""
    token = await db.get(Token, token_id)
    if not token:
        raise _DENY
    await _assert_token_access(token, user, ref)

    # Always regenerate: QR payload is a signed opaque reference whose
    # nonce/expiry must match the current signing scheme — older files
    # (pre-signed-QR scheme) are invalid for tracking.
    qr_path = await qr_generator.generate_token_qr(
        token_id=token.id, org_id=token.organization_id,
        counter_id=token.counter_id, user_id=token.user_id
    )
    token.qr_code_path = qr_path
    await db.commit()
    return FileResponse(qr_path, media_type="image/png",
                        filename=f"qr_{token.token_number}.png")
