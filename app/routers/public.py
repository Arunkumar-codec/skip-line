"""
app/routers/public.py
──────────────────────
Public endpoints that require no authentication:
- Kiosk token booking
- Live display screen data
- QR-code token tracking page
"""

from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.common import APIResponse
from app.schemas.kiosk import (
    KioskDeviceRead,
    KioskHeartbeatRequest,
    KioskPairExchangeRequest,
    KioskPairRequest,
)
from app.schemas.organization import (
    PublicSlotDayRead,
    SlotDateRead,
)
from app.schemas.token import KioskTokenRequest, TokenRead
from app.services import kiosk_service, schedule_service, token_service
from app.utils import signed_qr

templates = Jinja2Templates(directory="templates")

router = APIRouter()


# ── Kiosk token booking ───────────────────────────────────────────────────────

@router.post("/kiosk/book", response_model=APIResponse)
async def kiosk_book(
    body: KioskTokenRequest,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """
    Guest booking from a public kiosk.
    No login required — guest name and mobile are captured instead.
    Retries (printer/network failures) are already idempotent via
    `client_reference`; an API `Idempotency-Key` header is honored too.
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
            sub_service_ids=body.sub_service_ids,
            form_answers=body.form_answers,
            slot_id=body.slot_id,
            # Server-side issuing channel (SCH-004): never client input.
            booking_source="kiosk",
            client_reference=body.client_reference or idempotency_key,
        )
        # QR-001 hardening: attach the signed receipt-download ref in the
        # same response — without it, kiosk guests could never legitimately
        # print their receipt (guest receipts can no longer be fetched by
        # token id alone).
        payload = TokenRead.model_validate(token)
        payload.receipt_ref = token_service.guest_receipt_ref(token)
        return APIResponse(message="Token issued", data=payload)
    except ValueError as e:
        msg = str(e)
        if msg.startswith("idempotency conflict"):
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except PermissionError as e:
        # Phase 8 (ENF-002): org suspension / scoped restriction / block
        raise HTTPException(status_code=403, detail=str(e))


# ── Kiosk Device Pairing & Heartbeat (KSK-001..006) ───────────────────────────


@router.post("/kiosk/pair-request", response_model=APIResponse)
async def kiosk_pair_request(
    body: KioskPairRequest,
    db: AsyncSession = Depends(get_db),
):
    """Device requests a 6-digit numeric pairing PIN (Open Decision 7)."""
    pin = await kiosk_service.request_pairing_pin(db, body.device_code, body.device_name)
    return APIResponse(
        message="Pairing PIN generated",
        data={"pairing_code": pin, "device_code": body.device_code, "expires_in_minutes": 15},
    )


@router.post("/kiosk/pair-exchange", response_model=APIResponse)
async def kiosk_pair_exchange(
    body: KioskPairExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Device exchanges confirmed PIN for persistent API credentials."""
    try:
        device, api_key = await kiosk_service.exchange_pairing_pin(
            db, body.device_code, body.pairing_code
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return APIResponse(
        message="Device paired and credentials issued",
        data={"api_key": api_key, "device": KioskDeviceRead.model_validate(device)},
    )


@router.post("/kiosk/heartbeat", response_model=APIResponse)
async def kiosk_heartbeat(
    body: KioskHeartbeatRequest,
    db: AsyncSession = Depends(get_db),
):
    """Kiosk heartbeat tracking (KSK-006)."""
    try:
        device = await kiosk_service.record_heartbeat(db, body.device_code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return APIResponse(
        message="Heartbeat recorded",
        data={"device_code": device.device_code, "last_heartbeat_at": device.last_heartbeat_at},
    )


# ── Live display screen ───────────────────────────────────────────────────────

async def _authorize_display(
    db: AsyncSession, request: Request, org_id: int, key: Optional[str]
) -> bool:
    """
    Decision 20: a public display (read-only registered device) must present
    a valid display-access-key. An authenticated organization session for
    this exact org is accepted too, so operator preview works unchanged.
    """
    if key:
        from app.services import display_access_service

        if await display_access_service.is_key_installed(db, org_id, key):
            return True
        return False

    from app.utils.jwt import decode_token
    from models.organization import Organization, OrgStatus

    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    else:
        token = request.cookies.get("org_token")
    if not token:
        return False
    payload = decode_token(token)
    if not payload or payload.get("role") != "organization":
        return False
    try:
        org = await db.get(Organization, int(payload.get("sub")))
    except (TypeError, ValueError):
        return False
    return bool(org) and org.id == org_id and org.status == OrgStatus.APPROVED


def _display_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail="Display access key required (Decision 20)",
    )


@router.get("/display/{org_id}", response_model=APIResponse)
async def display_screen(
    org_id: int,
    request: Request,
    key: Optional[str] = Query(default=None, description="Display access key"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns current counter state for the public display screen.
    The display page subscribes to WebSocket for real-time updates;
    this endpoint provides the initial data load. Authorized with a
    display-access-key (install/rotate via the org portal) or the
    organization session.
    """
    if not await _authorize_display(db, request, org_id, key):
        raise _display_unauthorized()
    data = await token_service.get_display_data(db, org_id)
    return APIResponse(data=data)


# ── Public TV display page (keyed install) ───────────────────────────────────

@router.get("/display-view/{org_id}", response_class=HTMLResponse)
async def display_view_page(
    org_id: int,
    request: Request,
    key: str = Query(description="Display access key (Decision 20)"),
    mute: str = "",
    fs: str = "",
    db: AsyncSession = Depends(get_db),
):
    """
    Public TV display for installed screens: the only way a screen without
    an operator session renders the queue display. Requires and carries the
    display-access-key into all subsequent WS/API calls.
    """
    if not await _authorize_display(db, request, org_id, key):
        raise _display_unauthorized()

    from models.organization import Organization

    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    display_data = await token_service.get_display_data(db, org_id)
    return templates.TemplateResponse("public/display.html", {
        "request": request,
        "org_id": org_id,
        "org_name": org.name,
        "display_data": display_data,
        "announcement": display_data.get("announcement", ""),
        "ticker_text": display_data.get("announcement", ""),
        "display_key": key,
        "auto_fullscreen": fs == "1",
        "audio_muted": mute == "1",
    })


# ── Token tracking (via signed QR scan) ──────────────────────────────────────

@router.get("/track/{ref}", response_class=HTMLResponse)
async def track_page(ref: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Public live-tracking page (Release Gate 6). The reference is the
    HMAC-signed opaque value from the token QR; internal token ids are
    never accepted. Tampered/never-issued/expired refs render the same
    uninformative error so no token can be probed.
    """
    status, payload = signed_qr.verify_token_ref(ref, signed_qr.PURPOSE_TRACK)
    if status != signed_qr.STATUS_VALID:
        return HTMLResponse("<h3>Invalid or expired token link</h3>", status_code=404)

    from models.organization import Organization
    from models.token import Token

    token = await db.get(Token, payload.token_id)
    if not token or token.organization_id != payload.org_id:
        return HTMLResponse("<h3>Invalid or expired token link</h3>", status_code=404)

    org = await db.get(Organization, token.organization_id)
    try:
        tracking = await token_service.get_token_tracking(db, payload.token_id)
    except ValueError:
        return HTMLResponse("<h3>Invalid or expired token link</h3>", status_code=404)

    return templates.TemplateResponse("public/track.html", {
        "request": request,
        "ws_ref": ref,
        "token_number": tracking["token_number"],
        "org_name": org.name if org else "Organization",
        "tracking": tracking,
    })


@router.get("/tracking/{ref}", response_model=APIResponse)
async def track_token(ref: str, db: AsyncSession = Depends(get_db)):
    """
    Live tracking JSON for a specific token, addressed by its signed
    opaque reference (QR-002). Expired/tampered refs return 404 without
    distinguishing between states.
    """
    status, payload = signed_qr.verify_token_ref(ref, signed_qr.PURPOSE_TRACK)
    if status != signed_qr.STATUS_VALID:
        raise HTTPException(status_code=404, detail="Invalid or expired token link")

    from models.token import Token

    token = await db.get(Token, payload.token_id)
    if not token or token.organization_id != payload.org_id:
        raise HTTPException(status_code=404, detail="Invalid or expired token link")

    try:
        data = await token_service.get_token_tracking(db, payload.token_id)
        return APIResponse(data=data)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid or expired token link")


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


# ── Public slot-date discovery (Phase 3 Task 4) ───────────────────────────────

@router.get("/organizations/{org_id}/slot-dates", response_model=APIResponse)
async def org_slot_dates(
    org_id: int,
    days: int = Query(
        default=schedule_service.PUBLISH_HORIZON_DAYS,
        ge=1,
        le=schedule_service.MAX_ADVANCE_BOOKING_DAYS,
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Per-date bookability summary for a public booking flow (SRCH-001/002):
    for every date in the tenant-timezone horizon this returns the
    availability state (available/limited/full/closed/holiday/not_open),
    remaining slot capacity across active slot-mode counters (None for
    legacy day-queue tenants), and business hours. No authentication.
    """
    from models.organization import Organization, OrgStatus
    org = await db.get(Organization, org_id)
    if not org or org.status != OrgStatus.APPROVED:
        raise HTTPException(status_code=404, detail="Organization not found")
    try:
        summaries = await schedule_service.get_public_slot_dates(db, org, days=days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return APIResponse(data=[SlotDateRead.model_validate(s) for s in summaries])


# ── Public slot ladder for one date (SCH-005/SCH-009) ─────────────────────────

@router.get("/organizations/{org_id}/slots", response_model=APIResponse)
async def org_slots(
    org_id: int,
    date: Optional[date_type] = Query(
        default=None,
        description="Business date in the organization timezone "
        "(YYYY-MM-DD); defaults to the organization's today.",
    ),
    counter_id: Optional[int] = Query(default=None, ge=1),
    branch_id: Optional[int] = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
):
    """
    Bookable slot ladder for one business date (MASTER_REQUIREMENTS 7.1
    "select ... an available slot", kiosk live-capacity check): dates and
    business hours come from the slot-date calendar; this endpoint adds the
    per-slot times — rendered in the organization's/branch's IANA timezone
    (SCH-009) with the absolute UTC instants alongside. Slots that already
    started (same-day cutoff) are never listed; filled slots show
    remaining = 0 and is_bookable = false.
    """
    from models.branch import Branch
    from models.organization import Organization, OrgStatus

    org = await db.get(Organization, org_id)
    if not org or org.status != OrgStatus.APPROVED:
        raise HTTPException(status_code=404, detail="Organization not found")

    tenant_branch: Optional[Branch] = None
    if branch_id is not None:
        tenant_branch = await db.get(Branch, branch_id)
        if (
            tenant_branch is None
            or tenant_branch.organization_id != org.id
            or not tenant_branch.is_active
        ):
            raise HTTPException(status_code=404, detail="Branch not found")

    try:
        ladder = await schedule_service.get_public_slots_for_date(
            db, org, date or schedule_service.get_tenant_today(org, tenant_branch),
            branch=tenant_branch, counter_id=counter_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return APIResponse(data=PublicSlotDayRead.model_validate(ladder))
