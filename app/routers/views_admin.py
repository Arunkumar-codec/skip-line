"""
app/routers/views_admin.py
───────────────────────────
Jinja2 HTML page routes for the admin panel.
All routes verify admin JWT via cookie and pass template context.

Fixes applied:
  - login_submit now redirects to /admin/dashboard (not /admin/dashboard-ui)
  - pending_count passed in every context so sidebar badge works
  - q param added to users() route
  - Added missing routes: /tokens, /reports/revenue, /reports/queue, /settings
"""

import math
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import admin_service
from app.utils.jwt import create_access_token, decode_token
from models.admin import Admin
from models.organization import Organization, OrgStatus
from models.token import Token, TokenStatus
from models.user import User
from models.payment import Payment, PaymentStatus

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _redirect_login():
    return RedirectResponse("/admin/login", status_code=302)


async def _get_admin_or_redirect(request: Request, db: AsyncSession):
    """Extract admin from cookie — redirect to login if missing."""
    token = request.cookies.get("admin_token")
    if not token:
        return None, _redirect_login()
    payload = decode_token(token)
    if not payload or payload.get("role") != "admin":
        return None, _redirect_login()
    admin = await db.get(Admin, int(payload["sub"]))
    if not admin or not admin.is_active:
        return None, _redirect_login()
    return admin, None


async def _pending_count(db: AsyncSession) -> int:
    """Return the number of pending organizations for the sidebar badge."""
    return (await db.execute(
        select(func.count()).select_from(Organization)
        .where(Organization.status == OrgStatus.PENDING)
    )).scalar() or 0


# ── Login page ────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse("shared/login.html", {
        "request": request,
        "portal_name": "Admin",
        "login_action": "/admin/login/submit",
        "error": error,
    })


@router.post("/login/submit")
async def login_submit(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    admin = await admin_service.authenticate_admin(db, form["email"], form["password"])
    if not admin:
        return RedirectResponse("/admin/login?error=Invalid+credentials", status_code=302)
    token = create_access_token(admin.id, "admin")
    # FIX: redirect to /admin/dashboard (HTML view), not /admin/dashboard-ui (broken API route)
    resp = RedirectResponse("/admin/dashboard", status_code=302)
    resp.set_cookie("admin_token", token, httponly=True, samesite="lax", max_age=3600)
    return resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    admin, redir = await _get_admin_or_redirect(request, db)
    if redir:
        return redir

    stats = await admin_service.get_dashboard_stats(db)
    pc = await _pending_count(db)

    pending_result = await db.execute(
        select(Organization).where(Organization.status == OrgStatus.PENDING)
        .order_by(Organization.created_at.desc()).limit(10)
    )
    pending_orgs = pending_result.scalars().all()

    labels, data = [], []
    for i in range(6, -1, -1):
        day = datetime.now(timezone.utc) - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)
        count = (await db.execute(
            select(func.count()).select_from(Token).where(
                and_(Token.created_at >= day_start, Token.created_at < day_end)
            )
        )).scalar()
        labels.append(day.strftime("%a"))
        data.append(count)

    return templates.TemplateResponse("admin/dashboard.html", {
        "request": request,
        "current_admin": admin,
        "active_page": "dashboard",
        "pending_count": pc,
        "stats": stats,
        "pending_orgs": pending_orgs,
        "token_labels": labels,
        "token_data": data,
        "now": datetime.now(timezone.utc),
    })


# ── Organizations ─────────────────────────────────────────────────────────────

@router.get("/organizations", response_class=HTMLResponse)
async def organizations(
    request: Request,
    status: str = None,
    q: str = None,
    org_type: str = None,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    admin, redir = await _get_admin_or_redirect(request, db)
    if redir:
        return redir

    pc = await _pending_count(db)
    orgs, total = await admin_service.get_all_organizations(db, status=status, page=page, per_page=per_page)
    return templates.TemplateResponse("admin/organizations.html", {
        "request": request,
        "current_admin": admin,
        "active_page": "organizations",
        "pending_count": pc,
        "orgs": orgs,
        "total": total,
        "page": page,
        "pages": math.ceil(total / per_page) if total else 1,
        "status_filter": status,
        "type_filter": org_type,
        "q": q,
        "now": datetime.now(timezone.utc),
    })


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
async def users(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    q: str = None,          # FIX: added q param so template search box works
    db: AsyncSession = Depends(get_db),
):
    admin, redir = await _get_admin_or_redirect(request, db)
    if redir:
        return redir

    pc = await _pending_count(db)

    query = select(User)
    if q:
        from sqlalchemy import or_
        query = query.where(or_(User.name.ilike(f"%{q}%"), User.email.ilike(f"%{q}%")))
    total = (await db.execute(
        select(func.count()).select_from(query.subquery())
    )).scalar()
    result = await db.execute(
        query.order_by(User.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )
    users_list = result.scalars().all()

    return templates.TemplateResponse("admin/users.html", {
        "request": request,
        "current_admin": admin,
        "active_page": "users",
        "pending_count": pc,
        "users": users_list,
        "total": total,
        "page": page,
        "pages": math.ceil(total / per_page) if total else 1,
        "q": q,
        "now": datetime.now(timezone.utc),
    })


# ── Tokens ────────────────────────────────────────────────────────────────────
# NEW ROUTE — was missing; sidebar links to /admin/tokens

@router.get("/tokens", response_class=HTMLResponse)
async def tokens_page(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    status: str = None,
    db: AsyncSession = Depends(get_db),
):
    admin, redir = await _get_admin_or_redirect(request, db)
    if redir:
        return redir

    pc = await _pending_count(db)

    query = select(Token)
    if status:
        query = query.where(Token.status == status)
    total = (await db.execute(
        select(func.count()).select_from(query.subquery())
    )).scalar()
    result = await db.execute(
        query.order_by(Token.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )
    tokens_list = result.scalars().all()

    return templates.TemplateResponse("admin/tokens.html", {
        "request": request,
        "current_admin": admin,
        "active_page": "tokens",
        "pending_count": pc,
        "tokens": tokens_list,
        "total": total,
        "page": page,
        "pages": math.ceil(total / per_page) if total else 1,
        "status_filter": status,
        "now": datetime.now(timezone.utc),
    })


# ── Revenue Report ────────────────────────────────────────────────────────────
# NEW ROUTE — was missing; sidebar links to /admin/reports/revenue

@router.get("/reports/revenue", response_class=HTMLResponse)
async def revenue_report(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin, redir = await _get_admin_or_redirect(request, db)
    if redir:
        return redir

    pc = await _pending_count(db)

    # Last 30 days revenue by day
    labels, revenue_data = [], []
    for i in range(29, -1, -1):
        day = datetime.now(timezone.utc) - timedelta(days=i)
        ds  = day.replace(hour=0, minute=0, second=0, microsecond=0)
        de  = ds + timedelta(days=1)
        rev = (await db.execute(
            select(func.coalesce(func.sum(Payment.amount), 0))
            .where(and_(
                Payment.status == PaymentStatus.PAID,
                Payment.paid_at >= ds,
                Payment.paid_at < de,
            ))
        )).scalar() or 0
        labels.append(day.strftime("%d %b"))
        revenue_data.append(float(rev))

    # Total / monthly summary
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_revenue = (await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(and_(Payment.status == PaymentStatus.PAID, Payment.paid_at >= month_start))
    )).scalar() or 0

    total_revenue = (await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.status == PaymentStatus.PAID)
    )).scalar() or 0

    # Recent payments
    recent_payments = (await db.execute(
        select(Payment).where(Payment.status == PaymentStatus.PAID)
        .order_by(Payment.paid_at.desc()).limit(20)
    )).scalars().all()

    return templates.TemplateResponse("admin/reports_revenue.html", {
        "request": request,
        "current_admin": admin,
        "active_page": "revenue",
        "pending_count": pc,
        "labels": labels,
        "revenue_data": revenue_data,
        "monthly_revenue": float(monthly_revenue),
        "total_revenue": float(total_revenue),
        "recent_payments": recent_payments,
        "now": datetime.now(timezone.utc),
    })


# ── Queue Stats Report ────────────────────────────────────────────────────────
# NEW ROUTE — was missing; sidebar links to /admin/reports/queue

@router.get("/reports/queue", response_class=HTMLResponse)
async def queue_report(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    admin, redir = await _get_admin_or_redirect(request, db)
    if redir:
        return redir

    pc = await _pending_count(db)

    # Token status breakdown
    status_counts = {}
    for ts in TokenStatus:
        count = (await db.execute(
            select(func.count()).select_from(Token).where(Token.status == ts)
        )).scalar() or 0
        status_counts[ts.value] = count

    # Last 7 days issued vs completed
    labels, issued_data, completed_data = [], [], []
    for i in range(6, -1, -1):
        day = datetime.now(timezone.utc) - timedelta(days=i)
        ds  = day.replace(hour=0, minute=0, second=0, microsecond=0)
        de  = ds + timedelta(days=1)
        issued = (await db.execute(
            select(func.count()).select_from(Token)
            .where(and_(Token.created_at >= ds, Token.created_at < de))
        )).scalar() or 0
        completed = (await db.execute(
            select(func.count()).select_from(Token)
            .where(and_(Token.status == TokenStatus.COMPLETED,
                        Token.completed_at >= ds, Token.completed_at < de))
        )).scalar() or 0
        labels.append(day.strftime("%a"))
        issued_data.append(issued)
        completed_data.append(completed)

    total_tokens = sum(status_counts.values())

    return templates.TemplateResponse("admin/reports_queue.html", {
        "request": request,
        "current_admin": admin,
        "active_page": "queue_stats",
        "pending_count": pc,
        "status_counts": status_counts,
        "total_tokens": total_tokens,
        "labels": labels,
        "issued_data": issued_data,
        "completed_data": completed_data,
        "now": datetime.now(timezone.utc),
    })


# ── Settings ──────────────────────────────────────────────────────────────────
# NEW ROUTE — was missing; sidebar links to /admin/settings

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    success: str = None,
    error: str = None,
):
    admin, redir = await _get_admin_or_redirect(request, db)
    if redir:
        return redir

    pc = await _pending_count(db)
    return templates.TemplateResponse("admin/settings.html", {
        "request": request,
        "current_admin": admin,
        "active_page": "settings",
        "pending_count": pc,
        "success": success,
        "error": error,
        "now": datetime.now(timezone.utc),
    })


# ── Change password (HTML page) ───────────────────────────────────────────────

@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request,
    success: str = None,
    error: str = None,
    db: AsyncSession = Depends(get_db),
):
    admin, redir = await _get_admin_or_redirect(request, db)
    if redir:
        return redir

    pc = await _pending_count(db)
    return templates.TemplateResponse("admin/change_password.html", {
        "request": request,
        "current_admin": admin,
        "active_page": "change_password",
        "pending_count": pc,
        "success": success,
        "error": error,
        "now": datetime.now(timezone.utc),
    })


@router.post("/change-password/submit")
async def change_password_submit(request: Request, db: AsyncSession = Depends(get_db)):
    admin, redir = await _get_admin_or_redirect(request, db)
    if redir:
        return redir
    form = await request.form()
    if form["new_password"] != form["confirm_password"]:
        return RedirectResponse("/admin/change-password?error=Passwords+do+not+match", status_code=302)
    ok = await admin_service.change_admin_password(
        db, admin, form["current_password"], form["new_password"]
    )
    if not ok:
        return RedirectResponse("/admin/change-password?error=Current+password+incorrect", status_code=302)
    return RedirectResponse("/admin/change-password?success=1", status_code=302)


# ── Logout ────────────────────────────────────────────────────────────────────

@router.get("/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=302)
    resp.delete_cookie("admin_token")
    return resp
