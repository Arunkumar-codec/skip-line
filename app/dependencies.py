"""
app/dependencies.py
───────────────────
Reusable FastAPI dependencies for authentication and authorization.
Provides:
  - Unified AuthenticatedPrincipal context with RBAC permission resolution
  - require_permission(perm) dependency factory
  - Server-side session verification and token revocation
  - Backward-compatible role-specific dependencies:
      get_current_admin, get_current_org, get_current_employee, get_current_user
"""

from dataclasses import dataclass, field
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.utils.jwt import decode_token
from models.admin import Admin
from models.employee import Employee
from models.organization import Organization
from models.user import User

bearer_scheme = HTTPBearer(auto_error=False)


# ── Unified Authenticated Principal ──────────────────────────────────────────

@dataclass
class AuthenticatedPrincipal:
    id: int
    role: str  # "admin" | "organization" | "employee" | "user"
    email: str
    name: str
    organization_id: Optional[int] = None
    branch_id: Optional[int] = None
    is_active: bool = True
    permissions: set[str] = field(default_factory=set)
    session_id: Optional[str] = None


# ── Token extraction & Session validation ─────────────────────────────────────

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


async def _validate_session_if_present(db: AsyncSession, payload: dict) -> None:
    """Validate server-side session status if a session identifier is present."""
    session_id = payload.get("jti") or payload.get("session_id")
    if session_id:
        from app.services.session_service import get_valid_session

        session = await get_valid_session(db, session_id)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or revoked",
            )


# ── Unified Principal Dependency ──────────────────────────────────────────────

async def get_current_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedPrincipal:
    """
    Resolve the unified AuthenticatedPrincipal from Bearer token or session cookies.
    Verifies server-side session validity, active status, and resolves RBAC permissions.
    """
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    else:
        for cname in ("employee_token", "org_token", "admin_token", "user_token"):
            val = request.cookies.get(cname)
            if val:
                token = val
                break

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    payload = decode_token(token)
    if not payload or "sub" not in payload or "role" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )

    await _validate_session_if_present(db, payload)

    role = payload["role"]
    sub_id = int(payload["sub"])
    session_id = payload.get("jti") or payload.get("session_id")

    if role == "admin":
        admin = await db.get(Admin, sub_id)
        if not admin or not admin.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Admin not found or inactive",
            )
        return AuthenticatedPrincipal(
            id=admin.id,
            role="admin",
            email=admin.email,
            name=admin.name,
            is_active=admin.is_active,
            permissions={"*"},
            session_id=session_id,
        )

    elif role == "organization":
        org = await db.get(Organization, sub_id)
        if not org or org.status != "approved":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Organization not approved"
            )
        return AuthenticatedPrincipal(
            id=org.id,
            role="organization",
            email=org.email,
            name=org.name,
            organization_id=org.id,
            is_active=org.is_active,
            permissions={"*"},
            session_id=session_id,
        )

    elif role == "employee":
        employee = await db.get(Employee, sub_id)
        if not employee or not employee.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Employee not found or inactive",
            )
        from app.services.rbac_service import get_employee_permissions

        perms = await get_employee_permissions(db, employee.id)
        return AuthenticatedPrincipal(
            id=employee.id,
            role="employee",
            email=employee.email,
            name=employee.name,
            organization_id=employee.organization_id,
            branch_id=employee.branch_id,
            is_active=employee.is_active,
            permissions=perms,
            session_id=session_id,
        )

    elif role == "user":
        user = await db.get(User, sub_id)
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
            )
        return AuthenticatedPrincipal(
            id=user.id,
            role="user",
            email=user.email,
            name=user.name,
            is_active=user.is_active,
            permissions={"user:access"},
            session_id=session_id,
        )

    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Unknown principal role"
        )


def require_permission(perm: str):
    """Dependency factory ensuring the authenticated principal has the given permission."""
    async def dependency(
        principal: AuthenticatedPrincipal = Depends(get_current_principal),
    ) -> AuthenticatedPrincipal:
        if principal.role in ("admin", "organization"):
            return principal
        if perm in principal.permissions or "*" in principal.permissions:
            return principal
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {perm} required",
        )

    return dependency


# ── Role-Specific Dependencies (Backward Compatible) ──────────────────────────

async def get_current_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Admin:
    token = _extract_token(request, credentials, "admin_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    payload = decode_token(token)
    if not payload or payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
        )

    await _validate_session_if_present(db, payload)

    admin = await db.get(Admin, int(payload["sub"]))
    if not admin or not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin not found or inactive",
        )

    return admin


async def get_current_org(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Organization:
    token = _extract_token(request, credentials, "org_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    payload = decode_token(token)
    if not payload or payload.get("role") != "organization":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Organization access required"
        )

    await _validate_session_if_present(db, payload)

    org = await db.get(Organization, int(payload["sub"]))
    if not org or org.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Organization not approved"
        )

    return org


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = _extract_token(request, credentials, "user_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    payload = decode_token(token)
    if not payload or payload.get("role") != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User access required"
        )

    await _validate_session_if_present(db, payload)

    user = await db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive"
        )

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


async def get_current_employee(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Employee:
    token = _extract_token(request, credentials, "employee_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    payload = decode_token(token)
    if not payload or payload.get("role") != "employee":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Employee access required"
        )

    await _validate_session_if_present(db, payload)

    employee = await db.get(Employee, int(payload["sub"]))
    if not employee or not employee.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Employee not found or inactive",
        )

    return employee
