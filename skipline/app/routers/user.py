"""
app/routers/user.py
────────────────────
User REST API endpoints (JSON).
GET routes that conflicted with views_user.py HTML routes have been renamed
to /api/* so that the HTML view router always wins the first-match.
"""

import math
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.schemas.auth import ChangePasswordRequest
from app.schemas.common import APIResponse, MessageResponse, PaginatedResponse
from app.schemas.organization import OrgSearchResult
from app.schemas.token import TokenBookRequest, TokenRead
from app.schemas.user import UserRead, UserRegisterRequest, UserUpdateRequest
from app.services import user_service, token_service, notification_service
from models.user import User

router = APIRouter()


# ── Registration (public) ─────────────────────────────────────────────────────

@router.post("/register", response_model=APIResponse)
async def register(body: UserRegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        user = await user_service.register_user(db, body.model_dump())
        return APIResponse(message="Registration successful", data=UserRead.model_validate(user))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Dashboard / profile (JSON API) ───────────────────────────────────────────
# Renamed from /dashboard and /profile to /api/* to avoid shadowing HTML views.

@router.get("/api/dashboard", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def dashboard_api(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    active = await user_service.get_user_active_tokens(db, user.id)
    return APIResponse(data={
        "user": UserRead.model_validate(user),
        "active_tokens": [TokenRead.model_validate(t) for t in active],
        "active_count": len(active),
    })


@router.get("/api/profile", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def profile_api(user: User = Depends(get_current_user)):
    return APIResponse(data=UserRead.model_validate(user))


@router.put("/profile", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def update_profile(
    body: UserUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    updated = await user_service.update_user(db, user, body.model_dump(exclude_none=True))
    return APIResponse(data=UserRead.model_validate(updated))


@router.post("/change-password", response_model=MessageResponse, dependencies=[Depends(get_current_user)])
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    ok = await user_service.change_user_password(db, user, body.current_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail="Current password incorrect")
    return MessageResponse(message="Password changed")


# ── Organization search ───────────────────────────────────────────────────────

@router.get("/organizations/search", response_model=APIResponse)
async def search_orgs(
    q: str = None,
    org_type: str = None,
    city: str = None,
    state: str = None,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    orgs, total = await user_service.search_organizations(
        db, query=q, org_type=org_type, city=city, state=state, page=page, per_page=per_page
    )
    return PaginatedResponse(
        total=total, page=page, per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[OrgSearchResult.model_validate(o) for o in orgs],
    )


# ── Token booking ─────────────────────────────────────────────────────────────

@router.post("/tokens/book", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def book_token(
    body: TokenBookRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        token = await token_service.book_token(
            db, body.organization_id, body.counter_id, body.service_id, user.id
        )
        await notification_service.notify_token_created(db, token, user)
        await db.commit()
        return APIResponse(message="Token booked", data=TokenRead.model_validate(token))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Token history (JSON API) ──────────────────────────────────────────────────
# Renamed from /tokens/history to /api/tokens/history to avoid shadowing HTML view.

@router.get("/api/tokens/history", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def token_history_api(
    page: int = 1,
    per_page: int = 20,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tokens, total = await user_service.get_user_token_history(db, user.id, page, per_page)
    return PaginatedResponse(
        total=total, page=page, per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[TokenRead.model_validate(t) for t in tokens],
    )


# ── Live tracking ─────────────────────────────────────────────────────────────

@router.get("/api/tokens/{token_id}/track", response_model=APIResponse)
async def track_token(token_id: int, db: AsyncSession = Depends(get_db)):
    try:
        data = await token_service.get_token_tracking(db, token_id)
        return APIResponse(data=data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
