"""
app/routers/org.py
───────────────────
Organization REST API endpoints (JSON).
GET routes that conflicted with views_org.py HTML routes have been renamed
to /api/* so that the HTML view router always wins the first-match.
"""

import math
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_org
from app.schemas.auth import ChangePasswordRequest
from app.schemas.common import APIResponse, MessageResponse, PaginatedResponse
from app.schemas.counter import CounterCreate, CounterRead, CounterUpdate
from app.schemas.organization import OrgRead, OrgRegisterRequest, OrgUpdateRequest
from app.schemas.service import ServiceCreate, ServiceRead, ServiceUpdate
from app.schemas.token import TokenActionRequest, TokenRead
from app.services import org_service, token_service
from app.websocket.manager import ws_manager
from models.organization import Organization

router = APIRouter()


# ── Registration (public) ─────────────────────────────────────────────────────

@router.post("/register", response_model=APIResponse)
async def register(body: OrgRegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        org = await org_service.register_organization(db, body.model_dump())
        return APIResponse(message="Registration submitted. Pending admin approval.", data=OrgRead.model_validate(org))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Dashboard (JSON API) ──────────────────────────────────────────────────────
# Renamed from /dashboard to /api/dashboard to avoid shadowing the HTML view.

@router.get("/api/dashboard", dependencies=[Depends(get_current_org)])
async def dashboard_api(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    stats = await org_service.get_org_dashboard_stats(db, org.id)
    return APIResponse(data={"org": OrgRead.model_validate(org), "stats": stats})


# ── Profile ───────────────────────────────────────────────────────────────────
# Renamed GET /profile to /api/profile to avoid shadowing the HTML view.

@router.get("/api/profile", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_profile_api(org: Organization = Depends(get_current_org)):
    return APIResponse(data=OrgRead.model_validate(org))


@router.put("/profile", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_profile(
    body: OrgUpdateRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    data = body.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(org, k, v)
    await db.commit()
    await db.refresh(org)
    return APIResponse(data=OrgRead.model_validate(org))


@router.post("/change-password", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def change_password(
    body: ChangePasswordRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    ok = await org_service.change_org_password(db, org, body.current_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail="Current password incorrect")
    return MessageResponse(message="Password changed")


# ── Counters ──────────────────────────────────────────────────────────────────
# Renamed GET /counters to /api/counters to avoid shadowing the HTML view.

@router.get("/api/counters", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_counters_api(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    counters = await org_service.list_counters(db, org.id)
    return APIResponse(data=[CounterRead.model_validate(c) for c in counters])


@router.post("/counters", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_counter(
    body: CounterCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    counter = await org_service.create_counter(db, org.id, body.model_dump())
    return APIResponse(message="Counter created", data=CounterRead.model_validate(counter))


@router.put("/counters/{counter_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_counter(
    counter_id: int,
    body: CounterUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        counter = await org_service.update_counter(db, counter_id, org.id, body.model_dump(exclude_none=True))
        return APIResponse(data=CounterRead.model_validate(counter))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/counters/{counter_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_counter(
    counter_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await org_service.delete_counter(db, counter_id, org.id)
        return MessageResponse(message="Counter deleted")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Services ──────────────────────────────────────────────────────────────────
# Renamed GET /services to /api/services to avoid shadowing the HTML view.

@router.get("/api/services", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def list_services_api(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    services = await org_service.list_services(db, org.id)
    return APIResponse(data=[ServiceRead.model_validate(s) for s in services])


@router.post("/services", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def create_service(
    body: ServiceCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    service = await org_service.create_service(db, org.id, body.model_dump())
    return APIResponse(message="Service created", data=ServiceRead.model_validate(service))


@router.put("/services/{service_id}", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def update_service(
    service_id: int,
    body: ServiceUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        service = await org_service.update_service(db, service_id, org.id, body.model_dump(exclude_none=True))
        return APIResponse(data=ServiceRead.model_validate(service))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/services/{service_id}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def delete_service(
    service_id: int,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await org_service.delete_service(db, service_id, org.id)
        return MessageResponse(message="Service deleted")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Token actions ─────────────────────────────────────────────────────────────

@router.post("/tokens/{token_id}/action", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def token_action(
    token_id: int,
    body: TokenActionRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        token = await token_service.perform_token_action(
            db, token_id, org.id, body.action, "organization", org.id, body.notes
        )
        display = await token_service.get_display_data(db, org.id)
        await ws_manager.broadcast_to_org(org.id, {"type": "display_update", "data": display})
        tracking = await token_service.get_token_tracking(db, token_id)
        await ws_manager.broadcast_to_token(token_id, {"type": "tracking_update", "data": tracking})
        return APIResponse(message=f"Token {body.action}ed", data=TokenRead.model_validate(token))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Settings (JSON API) ───────────────────────────────────────────────────────
# Renamed GET /settings to /api/settings to avoid shadowing the HTML view.

@router.get("/api/settings", response_model=APIResponse, dependencies=[Depends(get_current_org)])
async def get_settings_api(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    settings_dict = await org_service.get_org_settings(db, org.id)
    return APIResponse(data=settings_dict)


@router.put("/settings/{key}", response_model=MessageResponse, dependencies=[Depends(get_current_org)])
async def update_setting(
    key: str,
    value: str,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    await org_service.upsert_setting(db, org.id, key, value)
    return MessageResponse(message="Setting updated")
