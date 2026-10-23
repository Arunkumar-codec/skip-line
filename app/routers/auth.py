"""
app/routers/auth.py
────────────────────
Login and logout endpoints for admin, organization, employee, and user roles.
Issues JWT with server-side session tracking, audit logging, and cookie support.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.auth import LoginRequest, TokenResponse, ChangePasswordRequest
from app.schemas.common import MessageResponse
from app.schemas.employee import EmployeeLoginRequest, EmployeeLoginResponse
from app.services import (
    admin_service,
    audit_service,
    employee_service,
    org_service,
    session_service,
    user_service,
)
from app.utils.jwt import create_access_token, decode_token

router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)

COOKIE_MAX_AGE = 60 * 60  # 1 hour


def _set_cookie(response: Response, cookie_name: str, token: str) -> None:
    response.set_cookie(
        key=cookie_name,
        value=token,
        httponly=True,
        max_age=COOKIE_MAX_AGE,
        samesite="lax",
    )


def _client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


# ── Admin login ───────────────────────────────────────────────────────────────

@router.post("/admin/login", response_model=TokenResponse)
async def admin_login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    admin = await admin_service.authenticate_admin(db, body.email, body.password)
    if not admin:
        await audit_service.log_audit_event(
            db,
            action="auth.admin.login",
            actor_type="admin",
            actor_email=body.email,
            outcome="failure",
            ip_address=ip,
            details={"reason": "Invalid credentials"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    session = await session_service.create_session(
        db,
        principal_type="admin",
        principal_id=admin.id,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )

    token = create_access_token(
        subject=admin.id,
        role="admin",
        extra_claims={"jti": session.id},
    )
    _set_cookie(response, "admin_token", token)

    await audit_service.log_audit_event(
        db,
        action="auth.admin.login",
        actor_type="admin",
        actor_id=admin.id,
        actor_email=admin.email,
        outcome="success",
        ip_address=ip,
        details={"session_id": session.id},
    )

    return TokenResponse(
        access_token=token, role="admin", entity_id=admin.id, name=admin.name
    )


# ── Organization login ────────────────────────────────────────────────────────

@router.post("/org/login", response_model=TokenResponse)
async def org_login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    org = await org_service.authenticate_org(db, body.email, body.password)
    if not org:
        await audit_service.log_audit_event(
            db,
            action="auth.org.login",
            actor_type="organization",
            actor_email=body.email,
            outcome="failure",
            ip_address=ip,
            details={"reason": "Invalid credentials or account not approved"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials or account not approved",
        )

    session = await session_service.create_session(
        db,
        principal_type="organization",
        principal_id=org.id,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )

    token = create_access_token(
        subject=org.id,
        role="organization",
        extra_claims={"jti": session.id, "org_id": org.id},
    )
    _set_cookie(response, "org_token", token)

    await audit_service.log_audit_event(
        db,
        action="auth.org.login",
        actor_type="organization",
        actor_id=org.id,
        actor_email=org.email,
        organization_id=org.id,
        outcome="success",
        ip_address=ip,
        details={"session_id": session.id},
    )

    return TokenResponse(
        access_token=token, role="organization", entity_id=org.id, name=org.name
    )


# ── User login ────────────────────────────────────────────────────────────────

@router.post("/user/login", response_model=TokenResponse)
async def user_login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    user = await user_service.authenticate_user(db, body.email, body.password)
    if not user:
        await audit_service.log_audit_event(
            db,
            action="auth.user.login",
            actor_type="user",
            actor_email=body.email,
            outcome="failure",
            ip_address=ip,
            details={"reason": "Invalid credentials"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    session = await session_service.create_session(
        db,
        principal_type="user",
        principal_id=user.id,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )

    token = create_access_token(
        subject=user.id,
        role="user",
        extra_claims={"jti": session.id},
    )
    _set_cookie(response, "user_token", token)

    await audit_service.log_audit_event(
        db,
        action="auth.user.login",
        actor_type="user",
        actor_id=user.id,
        actor_email=user.email,
        outcome="success",
        ip_address=ip,
        details={"session_id": session.id},
    )

    return TokenResponse(
        access_token=token, role="user", entity_id=user.id, name=user.name
    )


# ── Employee login ────────────────────────────────────────────────────────────

@router.post("/employee/login", response_model=EmployeeLoginResponse)
async def employee_login(
    body: EmployeeLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    employee = await employee_service.authenticate_employee(db, body.email, body.password)
    if not employee:
        await audit_service.log_audit_event(
            db,
            action="auth.employee.login",
            actor_type="employee",
            actor_email=body.email,
            outcome="failure",
            ip_address=ip,
            details={"reason": "Invalid credentials or inactive account"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials or inactive account",
        )

    session = await session_service.create_session(
        db,
        principal_type="employee",
        principal_id=employee.id,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )

    token = create_access_token(
        subject=employee.id,
        role="employee",
        extra_claims={
            "jti": session.id,
            "org_id": employee.organization_id,
            "branch_id": employee.branch_id,
        },
    )
    _set_cookie(response, "employee_token", token)

    await audit_service.log_audit_event(
        db,
        action="auth.employee.login",
        actor_type="employee",
        actor_id=employee.id,
        actor_email=employee.email,
        organization_id=employee.organization_id,
        outcome="success",
        ip_address=ip,
        details={"session_id": session.id, "branch_id": employee.branch_id},
    )

    return EmployeeLoginResponse(
        access_token=token,
        token_type="bearer",
        role="employee",
        entity_id=employee.id,
        organization_id=employee.organization_id,
        branch_id=employee.branch_id,
        name=employee.name,
        email=employee.email,
    )


# ── Logout (revokes session & clears cookie) ──────────────────────────────────

@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
):
    """
    Revokes the current server-side session and clears client cookies.
    Subsequent requests with the revoked token are immediately rejected.
    """
    # 1. Extract token
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    else:
        for cname in ("employee_token", "org_token", "admin_token", "user_token"):
            val = request.cookies.get(cname)
            if val:
                token = val
                break

    # 2. Revoke server-side session
    if token:
        payload = decode_token(token)
        if payload:
            session_id = payload.get("jti") or payload.get("session_id")
            if session_id:
                await session_service.revoke_session(db, session_id)
                await audit_service.log_audit_event(
                    db,
                    action="auth.logout",
                    actor_type=payload.get("role", "unknown"),
                    actor_id=int(payload["sub"]) if payload.get("sub") else None,
                    organization_id=payload.get("org_id"),
                    outcome="success",
                    ip_address=_client_ip(request),
                    details={"session_id": session_id},
                )

    # 3. Clear all potential cookies
    for cookie in ("admin_token", "org_token", "user_token", "employee_token"):
        response.delete_cookie(cookie)

    return MessageResponse(message="Logged out successfully")


@router.post("/logout-all", response_model=MessageResponse)
async def logout_all_devices(
    request: Request,
    response: Response,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
):
    """Revoke all active sessions for the current authenticated principal."""
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

    role = payload["role"]
    sub_id = int(payload["sub"])

    revoked_count = await session_service.revoke_all_sessions(db, role, sub_id)

    await audit_service.log_audit_event(
        db,
        action="auth.logout_all",
        actor_type=role,
        actor_id=sub_id,
        organization_id=payload.get("org_id"),
        outcome="success",
        ip_address=_client_ip(request),
        details={"revoked_sessions_count": revoked_count},
    )

    for cookie in ("admin_token", "org_token", "user_token", "employee_token"):
        response.delete_cookie(cookie)

    return MessageResponse(message=f"Logged out from all devices ({revoked_count} sessions revoked)")
