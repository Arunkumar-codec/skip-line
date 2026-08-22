"""
app/routers/public.py
──────────────────────
Public endpoints that require no authentication:
- Kiosk token booking
- Live display screen data
- QR-code token tracking page
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.common import APIResponse
from app.schemas.token import KioskTokenRequest, TokenRead
from app.services import token_service

router = APIRouter()


# ── Kiosk token booking ───────────────────────────────────────────────────────

@router.post("/kiosk/book", response_model=APIResponse)
async def kiosk_book(body: KioskTokenRequest, db: AsyncSession = Depends(get_db)):
    """
    Guest booking from a public kiosk.
    No login required — guest name and mobile are captured instead.
    """
    try:
        token = await token_service.book_token(
            db,
            org_id=body.organization_id,
            counter_id=body.counter_id,
            service_id=body.service_id,
            user_id=None,
            guest_name=body.guest_name,
            guest_mobile=body.guest_mobile,
        )
        return APIResponse(message="Token issued", data=TokenRead.model_validate(token))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Live display screen ───────────────────────────────────────────────────────

@router.get("/display/{org_id}", response_model=APIResponse)
async def display_screen(org_id: int, db: AsyncSession = Depends(get_db)):
    """
    Returns current counter state for the public display screen.
    The display page subscribes to WebSocket for real-time updates;
    this endpoint provides the initial data load.
    """
    data = await token_service.get_display_data(db, org_id)
    return APIResponse(data=data)


# ── Token tracking (via QR scan) ──────────────────────────────────────────────

@router.get("/track/{token_id}", response_model=APIResponse)
async def track_token(token_id: int, db: AsyncSession = Depends(get_db)):
    """
    Live tracking for a specific token.
    Accessible without login — QR code on the receipt links here.
    """
    try:
        data = await token_service.get_token_tracking(db, token_id)
        return APIResponse(data=data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Organization info (public listing) ────────────────────────────────────────

@router.get("/organizations/{org_id}", response_model=APIResponse)
async def org_info(org_id: int, db: AsyncSession = Depends(get_db)):
    from models.organization import Organization, OrgStatus
    from app.schemas.organization import OrgSearchResult
    org = await db.get(Organization, org_id)
    if not org or org.status != OrgStatus.APPROVED:
        raise HTTPException(status_code=404, detail="Organization not found")
    return APIResponse(data=OrgSearchResult.model_validate(org))


# ── Services list for an org (for kiosk / booking UI) ────────────────────────

@router.get("/organizations/{org_id}/services", response_model=APIResponse)
async def org_services(org_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select, and_
    from models.service import Service
    from app.schemas.service import ServiceRead
    result = await db.execute(
        select(Service).where(and_(Service.organization_id == org_id, Service.is_active == True))  # noqa: E712
    )
    services = result.scalars().all()
    return APIResponse(data=[ServiceRead.model_validate(s) for s in services])


@router.get("/organizations/{org_id}/counters", response_model=APIResponse)
async def org_counters(org_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select, and_
    from models.counter import Counter, CounterStatus
    from app.schemas.counter import CounterRead
    result = await db.execute(
        select(Counter).where(
            and_(Counter.organization_id == org_id, Counter.status == CounterStatus.ACTIVE)
        ).order_by(Counter.counter_number)
    )
    counters = result.scalars().all()
    return APIResponse(data=[CounterRead.model_validate(c) for c in counters])
