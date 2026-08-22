"""
app/routers/auth.py
────────────────────
Login and logout endpoints for admin, organization, and user roles.
Returns a JWT and sets a signed cookie for browser clients.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.auth import LoginRequest, TokenResponse, ChangePasswordRequest
from app.schemas.common import MessageResponse
from app.services import admin_service, org_service, user_service
from app.utils.jwt import create_access_token

router = APIRouter()

COOKIE_MAX_AGE = 60 * 60  # 1 hour


def _set_cookie(response: Response, cookie_name: str, token: str) -> None:
    response.set_cookie(
        key=cookie_name,
        value=token,
        httponly=True,
        max_age=COOKIE_MAX_AGE,
        samesite="lax",
    )


# ── Admin login ───────────────────────────────────────────────────────────────

@router.post("/admin/login", response_model=TokenResponse)
async def admin_login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    admin = await admin_service.authenticate_admin(db, body.email, body.password)
    if not admin:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token(subject=admin.id, role="admin")
    _set_cookie(response, "admin_token", token)
    return TokenResponse(access_token=token, role="admin", entity_id=admin.id, name=admin.name)


# ── Organization login ────────────────────────────────────────────────────────

@router.post("/org/login", response_model=TokenResponse)
async def org_login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    org = await org_service.authenticate_org(db, body.email, body.password)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials or account not approved",
        )

    token = create_access_token(subject=org.id, role="organization")
    _set_cookie(response, "org_token", token)
    return TokenResponse(access_token=token, role="organization", entity_id=org.id, name=org.name)


# ── User login ────────────────────────────────────────────────────────────────

@router.post("/user/login", response_model=TokenResponse)
async def user_login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    user = await user_service.authenticate_user(db, body.email, body.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token(subject=user.id, role="user")
    _set_cookie(response, "user_token", token)
    return TokenResponse(access_token=token, role="user", entity_id=user.id, name=user.name)


# ── Logout (clears cookie) ────────────────────────────────────────────────────

@router.post("/logout", response_model=MessageResponse)
async def logout(response: Response):
    for cookie in ("admin_token", "org_token", "user_token"):
        response.delete_cookie(cookie)
    return MessageResponse(message="Logged out successfully")
