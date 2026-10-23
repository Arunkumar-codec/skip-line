"""
app/routers/views_employee.py
─────────────────────────────
Jinja2 HTML page routes for the Employee / Counter Staff portal.
Includes dedicated employee login, station dashboard, and session management.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import audit_service, employee_service, rbac_service, session_service
from app.utils.jwt import create_access_token, decode_token
from models.branch import Branch
from models.employee import Employee

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _redirect_employee_login():
    return RedirectResponse("/employee/login", status_code=302)


async def _get_employee_or_redirect(request: Request, db: AsyncSession):
    token = request.cookies.get("employee_token")
    if not token:
        return None, _redirect_employee_login()

    payload = decode_token(token)
    if not payload or payload.get("role") != "employee":
        return None, _redirect_employee_login()

    session_id = payload.get("jti") or payload.get("session_id")
    if session_id:
        valid_sess = await session_service.get_valid_session(db, session_id)
        if not valid_sess:
            return None, _redirect_employee_login()

    employee = await db.get(Employee, int(payload["sub"]))
    if not employee or not employee.is_active:
        return None, _redirect_employee_login()

    return employee, None


# ── Auth Pages ────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def employee_login_page(request: Request, error: str = None):
    return templates.TemplateResponse(
        "shared/login.html",
        {
            "request": request,
            "portal_name": "Staff & Counter Operator",
            "login_action": "/employee/login/submit",
            "back_url": "/",
            "error": error,
        },
    )


@router.post("/login/submit")
async def employee_login_submit(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    email = form.get("email", "").strip()
    password = form.get("password", "")

    employee = await employee_service.authenticate_employee(db, email, password)
    if not employee:
        await audit_service.log_audit_event(
            db,
            action="auth.employee.login",
            actor_type="employee",
            actor_email=email,
            outcome="failure",
            details={"channel": "web_ui", "reason": "Invalid credentials or inactive"},
        )
        return RedirectResponse(
            "/employee/login?error=Invalid+credentials+or+inactive+account",
            status_code=302,
        )

    session = await session_service.create_session(
        db,
        principal_type="employee",
        principal_id=employee.id,
        ip_address=request.client.host if request.client else None,
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

    await audit_service.log_audit_event(
        db,
        action="auth.employee.login",
        actor_type="employee",
        actor_id=employee.id,
        actor_email=employee.email,
        organization_id=employee.organization_id,
        outcome="success",
        details={"channel": "web_ui", "session_id": session.id},
    )

    resp = RedirectResponse("/employee/dashboard", status_code=302)
    resp.set_cookie("employee_token", token, httponly=True, samesite="lax", max_age=3600)
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
async def employee_dashboard_page(request: Request, db: AsyncSession = Depends(get_db)):
    employee, redir = await _get_employee_or_redirect(request, db)
    if redir:
        return redir

    branch = await db.get(Branch, employee.branch_id)
    roles = await rbac_service.get_employee_roles(db, employee.organization_id, employee.id)
    permissions = await rbac_service.get_employee_permissions(db, employee.id)

    return templates.TemplateResponse(
        "employee/dashboard.html",
        {
            "request": request,
            "current_employee": employee,
            "branch": branch,
            "roles": roles,
            "permissions": sorted(list(permissions)),
        },
    )


@router.get("/logout")
async def employee_logout(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("employee_token")
    if token:
        payload = decode_token(token)
        if payload:
            session_id = payload.get("jti") or payload.get("session_id")
            if session_id:
                await session_service.revoke_session(db, session_id)

    resp = RedirectResponse("/employee/login", status_code=302)
    resp.delete_cookie("employee_token")
    return resp
