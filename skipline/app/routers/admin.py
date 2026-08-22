"""
app/routers/admin.py
─────────────────────
Admin-only REST API endpoints (JSON).
GET routes that had path conflicts with views_admin.py have been renamed
to /api/* so that the HTML view router always wins the first-match.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_admin
from app.schemas.auth import ChangePasswordRequest
from app.schemas.common import APIResponse, MessageResponse, PaginatedResponse
from app.schemas.organization import OrgRead
from app.services import admin_service
from models.admin import Admin

router = APIRouter(dependencies=[Depends(get_current_admin)])


# ── Dashboard (JSON API) ──────────────────────────────────────────────────────
# Renamed from /dashboard to /api/dashboard to avoid shadowing the HTML view.

@router.get("/api/dashboard")
async def dashboard_api(
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    stats = await admin_service.get_dashboard_stats(db)
    return APIResponse(data={"admin": {"id": admin.id, "name": admin.name}, "stats": stats})


# ── Organizations (JSON API) ──────────────────────────────────────────────────
# Renamed from /organizations to /api/organizations.

@router.get("/api/organizations")
async def list_organizations_api(
    status: str = None,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    orgs, total = await admin_service.get_all_organizations(db, status=status, page=page, per_page=per_page)
    import math
    return PaginatedResponse(
        total=total, page=page, per_page=per_page,
        pages=math.ceil(total / per_page),
        data=[OrgRead.model_validate(o) for o in orgs],
    )


@router.post("/organizations/{org_id}/approve", response_model=APIResponse)
async def approve_org(
    org_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        org = await admin_service.approve_organization(db, org_id, admin)
        return APIResponse(message="Organization approved", data=OrgRead.model_validate(org))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/organizations/{org_id}/reject", response_model=MessageResponse)
async def reject_org(
    org_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        await admin_service.reject_organization(db, org_id, admin)
        return MessageResponse(message="Organization rejected")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/organizations/{org_id}/suspend", response_model=MessageResponse)
async def suspend_org(
    org_id: int,
    db: AsyncSession = Depends(get_db),
):
    try:
        await admin_service.suspend_organization(db, org_id)
        return MessageResponse(message="Organization suspended")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/organizations/{org_id}", response_model=MessageResponse)
async def delete_org(
    org_id: int,
    db: AsyncSession = Depends(get_db),
):
    try:
        await admin_service.delete_organization(db, org_id)
        return MessageResponse(message="Organization deleted")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Users (JSON API) ──────────────────────────────────────────────────────────
# Renamed from /users to /api/users.

@router.get("/api/users")
async def list_users_api(
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select, func
    from models.user import User
    total = (await db.execute(select(func.count()).select_from(User))).scalar()
    result = await db.execute(
        select(User).order_by(User.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )
    users = result.scalars().all()
    from app.schemas.user import UserRead
    import math
    return PaginatedResponse(
        total=total, page=page, per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[UserRead.model_validate(u) for u in users],
    )


@router.post("/users/{user_id}/toggle", response_model=MessageResponse)
async def toggle_user(user_id: int, db: AsyncSession = Depends(get_db)):
    from models.user import User
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = not user.is_active
    await db.commit()
    return MessageResponse(message=f"User {'activated' if user.is_active else 'suspended'}")


# ── Change password (JSON API) ────────────────────────────────────────────────

@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    body: ChangePasswordRequest,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    ok = await admin_service.change_admin_password(db, admin, body.current_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    return MessageResponse(message="Password changed successfully")
