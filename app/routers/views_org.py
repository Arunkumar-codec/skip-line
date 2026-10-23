"""
app/routers/views_org.py
─────────────────────────
Jinja2 HTML page routes for the organization portal.

Fixes applied:
  ISSUE-05: dashboard() — added selectinload(Token.service) for recent_tokens
            so org/dashboard.html can access t.service.name safely.
  ISSUE-01: reports_page() — completely rewired to pass all variables that
            org/reports.html expects:
              daily_labels, daily_data, status_breakdown (dict),
              top_services (list of {name, count, revenue}),
              counter_stats (list of {name, served, avg_min})
  ISSUE-02: payments_page() — now passes total_revenue (float) and pages (int)
            that org/payments.html requires.
"""

import math
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, and_, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.services import (
    branch_service,
    employee_service,
    org_service,
    payment_service,
    rbac_service,
    token_service,
)
from app.utils.jwt import create_access_token, decode_token
from models.counter import Counter, CounterStatus
from models.organization import Organization, OrgStatus
from models.payment import Payment, PaymentStatus
from models.service import Service
from models.token import Token, TokenStatus

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _redirect_login():
    return RedirectResponse("/org/login", status_code=302)


async def _get_org_or_redirect(request: Request, db: AsyncSession):
    token = request.cookies.get("org_token")
    if not token:
        return None, _redirect_login()
    payload = decode_token(token)
    if not payload or payload.get("role") != "organization":
        return None, _redirect_login()
    org = await db.get(Organization, int(payload["sub"]))
    if not org or org.status != OrgStatus.APPROVED:
        return None, _redirect_login()
    return org, None


# ── Auth ──────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse("shared/login.html", {
        "request": request,
        "portal_name": "Organization",
        "login_action": "/org/login/submit",
        "register_url": "/org/register",
        "error": error,
    })


@router.post("/login/submit")
async def login_submit(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    org = await org_service.authenticate_org(db, form["email"], form["password"])
    if not org:
        return RedirectResponse("/org/login?error=Invalid+credentials+or+not+approved", status_code=302)
    token = create_access_token(org.id, "organization")
    resp = RedirectResponse("/org/dashboard", status_code=302)
    resp.set_cookie("org_token", token, httponly=True, samesite="lax", max_age=3600)
    return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = None, success: str = None):
    return templates.TemplateResponse("org/register.html", {
        "request": request,
        "error": error,
        "success": success,
    })


@router.post("/register/submit")
async def register_submit(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    data = dict(form)
    try:
        await org_service.register_organization(db, data)
        return RedirectResponse(
            "/org/login?success=Registration+submitted.+Await+admin+approval.", status_code=302
        )
    except ValueError as e:
        return RedirectResponse(f"/org/register?error={str(e)}", status_code=302)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    stats    = await org_service.get_org_dashboard_stats(db, org.id)
    counters = await org_service.list_counters(db, org.id)

    # FIX (ISSUE-05): Eagerly load Token.service so org/dashboard.html can
    # access t.service.name without triggering async lazy-load errors.
    recent = (await db.execute(
        select(Token)
        .where(Token.organization_id == org.id)
        .options(selectinload(Token.service))
        .order_by(Token.created_at.desc())
        .limit(10)
    )).scalars().all()

    return templates.TemplateResponse("org/dashboard.html", {
        "request": request,
        "current_org": org,
        "active_page": "dashboard",
        "stats": stats,
        "counters": counters,
        "recent_tokens": recent,
        "now": datetime.now(timezone.utc),
    })


# ── Queue management ──────────────────────────────────────────────────────────

@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    counters = await org_service.list_counters(db, org.id)
    return templates.TemplateResponse("org/queue.html", {
        "request": request,
        "current_org": org,
        "active_page": "queue",
        "counters": counters,
    })


@router.get("/queue-data/{counter_id}")
async def queue_data(counter_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """JSON endpoint consumed by the queue management JS."""
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    waiting = (await db.execute(
        select(Token).where(and_(
            Token.counter_id == counter_id,
            Token.organization_id == org.id,
            Token.status.in_([TokenStatus.WAITING, TokenStatus.ON_HOLD]),
        )).order_by(Token.sequence)
    )).scalars().all()

    current = (await db.execute(
        select(Token).where(and_(
            Token.counter_id == counter_id,
            Token.organization_id == org.id,
            Token.status.in_([TokenStatus.CALLED, TokenStatus.IN_PROGRESS]),
        )).order_by(Token.called_at.desc()).limit(1)
    )).scalar_one_or_none()

    completed = (await db.execute(
        select(Token).where(and_(
            Token.counter_id == counter_id,
            Token.organization_id == org.id,
            Token.status == TokenStatus.COMPLETED,
            Token.completed_at >= today,
        )).order_by(Token.completed_at.desc()).limit(30)
    )).scalars().all()

    def t_dict(t):
        return {
            "id": t.id, "token_number": t.token_number, "status": t.status,
            "queue_position": t.queue_position, "user_id": t.user_id,
            "guest_name": t.guest_name, "guest_mobile": t.guest_mobile,
            "booked_at": t.booked_at.isoformat() if t.booked_at else None,
            "called_at": t.called_at.isoformat() if t.called_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }

    return JSONResponse({
        "waiting":   [t_dict(t) for t in waiting],
        "current":   t_dict(current) if current else None,
        "completed": [t_dict(t) for t in completed],
    })


# ── Display screen ────────────────────────────────────────────────────────────

@router.get("/display", response_class=HTMLResponse)
async def display_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    display_data = await token_service.get_display_data(db, org.id)
    return templates.TemplateResponse("public/display.html", {
        "request": request,
        "org_id": org.id,
        "org_name": org.name,
        "display_data": display_data,
        "announcement": display_data.get("announcement", ""),
    })


# ── Counters ──────────────────────────────────────────────────────────────────

@router.get("/counters", response_class=HTMLResponse)
async def counters_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    counters = await org_service.list_counters(db, org.id)
    services = await org_service.list_services(db, org.id)
    # FIX (ISSUE-A): {{ c | tojson }} in counters.html fails on ORM objects.
    # Pass a plain-dict list that Jinja2 can serialise without error.
    counters_json = [
        {
            "id": c.id,
            "counter_name": c.counter_name,
            "counter_number": c.counter_number,
            "token_prefix": c.token_prefix,
            "daily_limit": c.daily_limit,
            "status": c.status if isinstance(c.status, str) else c.status.value,
            "service_id": c.service_id,
        }
        for c in counters
    ]
    return templates.TemplateResponse("org/counters.html", {
        "request": request,
        "current_org": org,
        "active_page": "counters",
        "counters": counters,
        "counters_json": counters_json,
        "services": services,
    })


# ── Services ──────────────────────────────────────────────────────────────────

@router.get("/services", response_class=HTMLResponse)
async def services_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    services = await org_service.list_services(db, org.id)
    # FIX (ISSUE-B): {{ s | tojson }} in services.html fails on ORM objects.
    # Pass a plain-dict list for JS consumption.
    services_json = [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description or "",
            "price": float(s.price),
            "estimated_minutes": s.estimated_minutes,
            "is_active": s.is_active,
        }
        for s in services
    ]
    return templates.TemplateResponse("org/services.html", {
        "request": request,
        "current_org": org,
        "active_page": "services",
        "services": services,
        "services_json": services_json,
    })


# ── Profile ───────────────────────────────────────────────────────────────────

@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    return templates.TemplateResponse("org/profile.html", {
        "request": request,
        "current_org": org,
        "active_page": "profile",
    })


# ── Payments ──────────────────────────────────────────────────────────────────

@router.get("/payments", response_class=HTMLResponse)
async def payments_page(
    request: Request,
    page: int = 1,
    db: AsyncSession = Depends(get_db),
):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    per_page = 20
    payments, total = await payment_service.get_org_payment_report(db, org.id, page, per_page)

    # FIX (ISSUE-02): org/payments.html requires total_revenue (float) and
    # pages (int). Previously only total (record count) was passed; the
    # template was crashing on format(total_revenue) with TypeError.
    total_revenue = await payment_service.get_org_total_revenue(db, org.id)

    return templates.TemplateResponse("org/payments.html", {
        "request": request,
        "current_org": org,
        "active_page": "payments",
        "payments": payments,
        "total": total,
        "total_revenue": total_revenue,
        "page": page,
        "pages": math.ceil(total / per_page) if total else 1,
    })


# ── Reports ───────────────────────────────────────────────────────────────────

@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, db: AsyncSession = Depends(get_db)):
    """
    FIX (ISSUE-01): org/reports.html uses these context variables:
        daily_labels        list[str]   — x-axis labels for bar chart
        daily_data          list[int]   — token counts per day
        status_breakdown    dict        — {status_str: count}
        top_services        list[dict]  — [{name, count, revenue}]
        counter_stats       list[dict]  — [{name, served, avg_min}]
        stats               dict        — org dashboard stats (total_tokens etc.)

    Previously the route passed token_labels/token_counts/completed_counts
    (wrong names) and omitted status_breakdown, top_services, counter_stats
    entirely — causing Jinja2 UndefinedError crashes and empty charts.
    """
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    stats = await org_service.get_org_dashboard_stats(db, org.id)

    # ── 1. Last 7 days daily token volume ────────────────────────────────────
    daily_labels, daily_data = [], []
    for i in range(6, -1, -1):
        day = datetime.now(timezone.utc) - timedelta(days=i)
        ds  = day.replace(hour=0, minute=0, second=0, microsecond=0)
        de  = ds + timedelta(days=1)
        cnt = (await db.execute(
            select(func.count()).select_from(Token).where(
                and_(Token.organization_id == org.id,
                     Token.created_at >= ds, Token.created_at < de)
            )
        )).scalar() or 0
        daily_labels.append(day.strftime("%a"))
        daily_data.append(cnt)

    # ── 2. Token status breakdown ─────────────────────────────────────────────
    status_breakdown: dict[str, int] = {}
    for ts in TokenStatus:
        count = (await db.execute(
            select(func.count()).select_from(Token).where(
                and_(Token.organization_id == org.id, Token.status == ts)
            )
        )).scalar() or 0
        if count > 0:
            status_breakdown[ts.value] = count
    if not status_breakdown:
        status_breakdown = {"waiting": 0}  # prevent empty dict in chart

    # ── 3. Top services ───────────────────────────────────────────────────────
    services = await org_service.list_services(db, org.id)
    top_services = []
    for svc in services[:6]:
        token_count = (await db.execute(
            select(func.count()).select_from(Token).where(
                and_(Token.organization_id == org.id, Token.service_id == svc.id)
            )
        )).scalar() or 0
        revenue = (await db.execute(
            select(func.coalesce(func.sum(Payment.amount), 0))
            .where(and_(Payment.organization_id == org.id,
                        Payment.status == PaymentStatus.PAID,
                        Token.service_id == svc.id))
            .join(Token, Payment.token_id == Token.id)
        )).scalar() or 0
        top_services.append({
            "name": svc.name,
            "count": token_count,
            "revenue": float(revenue),
        })
    # Sort by token count descending
    top_services.sort(key=lambda x: x["count"], reverse=True)

    # ── 4. Counter performance ────────────────────────────────────────────────
    counters = await org_service.list_counters(db, org.id)
    counter_stats = []
    for c in counters:
        served = (await db.execute(
            select(func.count()).select_from(Token).where(
                and_(Token.counter_id == c.id,
                     Token.status == TokenStatus.COMPLETED)
            )
        )).scalar() or 0

        avg_sec = (await db.execute(
            select(func.avg(
                func.timestampdiff(
                    text("SECOND"),
                    Token.called_at,
                    Token.completed_at,
                )
            )).where(
                and_(Token.counter_id == c.id,
                     Token.status == TokenStatus.COMPLETED,
                     Token.called_at.isnot(None),
                     Token.completed_at.isnot(None))
            )
        )).scalar()

        avg_min = round((float(avg_sec or 0)) / 60, 1)
        counter_stats.append({
            "name": c.counter_name,
            "served": served,
            "avg_min": avg_min,
        })

    return templates.TemplateResponse("org/reports.html", {
        "request": request,
        "current_org": org,
        "active_page": "reports",
        "stats": stats,
        "daily_labels": daily_labels,
        "daily_data": daily_data,
        "status_breakdown": status_breakdown,
        "top_services": top_services,
        "counter_stats": counter_stats,
    })


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    org_settings = await org_service.get_org_settings(db, org.id)
    return templates.TemplateResponse("org/settings.html", {
        "request": request,
        "current_org": org,
        "active_page": "settings",
        "org_settings": org_settings,
    })


# ── Form POST handlers ────────────────────────────────────────────────────────

@router.post("/profile/submit")
async def update_profile_submit(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir
    form = await request.form()
    for field in ("name", "address", "city", "state", "mobile", "description"):
        val = form.get(field)
        if val:
            setattr(org, field, val)
    await db.commit()
    return RedirectResponse("/org/profile?success=1", status_code=302)


@router.post("/change-password/submit")
async def change_password_submit(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir
    form = await request.form()
    if form["new_password"] != form["confirm_password"]:
        return RedirectResponse("/org/profile?error=Passwords+do+not+match", status_code=302)
    ok = await org_service.change_org_password(db, org, form["current_password"], form["new_password"])
    if not ok:
        return RedirectResponse("/org/profile?error=Current+password+incorrect", status_code=302)
    return RedirectResponse("/org/profile?success=1", status_code=302)


# ── Branches & Staff UI ───────────────────────────────────────────────────────

@router.get("/branches", response_class=HTMLResponse)
async def branches_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir
    branches = await branch_service.list_branches(db, org.id)
    branches_json = [
        {
            "id": b.id,
            "name": b.name,
            "branch_code": b.branch_code,
            "address": b.address,
            "city": b.city,
            "state": b.state,
            "pincode": b.pincode,
            "is_active": b.is_active,
        }
        for b in branches
    ]
    return templates.TemplateResponse(
        "org/branches.html",
        {
            "request": request,
            "current_org": org,
            "active_page": "branches",
            "branches": branches,
            "branches_json": branches_json,
        },
    )


@router.get("/employees", response_class=HTMLResponse)
async def employees_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir
    branches = await branch_service.list_branches(db, org.id)
    branch_map = {b.id: f"{b.name} ({b.branch_code})" for b in branches}
    employees = await employee_service.list_employees(db, org.id)
    available_roles = await rbac_service.list_roles(db, org.id)

    emp_roles_map = {}
    for emp in employees:
        roles = await rbac_service.get_employee_roles(db, org.id, emp.id)
        emp_roles_map[emp.id] = roles

    employees_json = [
        {
            "id": e.id,
            "name": e.name,
            "email": e.email,
            "employee_code": e.employee_code,
            "branch_id": e.branch_id,
            "is_active": e.is_active,
        }
        for e in employees
    ]
    return templates.TemplateResponse(
        "org/employees.html",
        {
            "request": request,
            "current_org": org,
            "active_page": "employees",
            "branches": branches,
            "branch_map": branch_map,
            "employees": employees,
            "employees_json": employees_json,
            "available_roles": available_roles,
            "emp_roles_map": emp_roles_map,
        },
    )


# ── Logout ────────────────────────────────────────────────────────────────────

@router.get("/logout")
async def logout():
    resp = RedirectResponse("/org/login", status_code=302)
    resp.delete_cookie("org_token")
    return resp
