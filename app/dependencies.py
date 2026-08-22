"""
app/dependencies.py
───────────────────
Reusable FastAPI dependencies for authentication and authorization.

Usage in routers:
    @router.get("/dashboard")
    async def dashboard(admin = Depends(get_current_admin)):
        ...
"""

from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.utils.jwt import decode_token
from models.admin import Admin
from models.organization import Organization
from models.user import User

bearer_scheme = HTTPBearer(auto_error=False)

# ── Token extraction ──────────────────────────────────────────────────────────

def _extract_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
    cookie_name: str,
) -> Optional[str]:
    """
    Token priority:
      1. Authorization: Bearer <token>  (API clients)
      2. Signed session cookie           (browser clients)
    """
    if credentials and credentials.credentials:
        return credentials.credentials
    return request.cookies.get(cookie_name)


# ── Admin ─────────────────────────────────────────────────────────────────────

async def get_current_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Admin:
    token = _extract_token(request, credentials, "admin_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_token(token)
    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    admin = await db.get(Admin, int(payload["sub"]))
    if not admin or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin not found or inactive")

    return admin


# ── Organization ──────────────────────────────────────────────────────────────

async def get_current_org(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Organization:
    token = _extract_token(request, credentials, "org_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_token(token)
    if not payload or payload.get("role") != "organization":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization access required")

    org = await db.get(Organization, int(payload["sub"]))
    if not org or org.status != "approved":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization not approved")

    return org


# ── User ──────────────────────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = _extract_token(request, credentials, "user_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_token(token)
    if not payload or payload.get("role") != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User access required")

    user = await db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    return user


async def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """Returns the current user or None — for pages accessible to guests."""
    try:
        return await get_current_user(request, credentials, db)
    except HTTPException:
        return None
