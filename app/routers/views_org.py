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
from datetime import datetime, timezone, timedelta, date as dt_date, time as dt_time
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, and_, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.services import (
    bank_account_service,
    branch_service,
    cash_service,
    employee_service,
    ledger_service,
    org_service,
    payment_service,
    payout_service,
    rbac_service,
    token_service,
)
from app.services import schedule_service
from app.utils.jwt import create_access_token, decode_token
from models.counter import Counter, CounterStatus
from models.organization import Organization, OrgStatus
from models.payment import Payment, PaymentStatus
from models.service import Service
from models.token import Token, TokenStatus
from models.weekly_schedule import ScheduleScopeLevel

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
    branches = await branch_service.list_branches(db, org.id)
    counters_json = [
        {
            "id": c.id,
            "counter_name": c.counter_name,
            "counter_number": c.counter_number,
            "token_prefix": c.token_prefix,
            "daily_limit": c.daily_limit,
            "status": c.status if isinstance(c.status, str) else c.status.value,
            "service_id": c.service_id,
            "branch_id": c.branch_id,
            "service_ids": [cs.service_id for cs in c.counter_services] if hasattr(c, "counter_services") and c.counter_services else (getattr(c, "service_ids", []) or []),
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
        "branches": branches,
    })


# ── Schedule manager (Phase 3: weekly operating hours + holiday calendar) ────
#
# Server-rendered panel wired to the SAME schedule_service CRUD the org API
# uses — Decision 17 honored (configuration rows only; generated time_slots
# and existing bookings are never touched from the panel). The panel scope is
# deliberately organization-level (the roadmap bullet's "weekly operating
# hours & holiday calendar"); branch/counter/service scoped schedules and
# capacity rules stay on the /org/schedules/* API endpoints. Edits that
# affect confirmed tokens must run the SCH-007 impact workflow
# (/org/schedules/changes/*) — the panel links to it instead of bypassing it.

_SCHEDULE_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                  "Saturday", "Sunday"]


def _weekly_row_json(row):
    return {
        "id": row.id,
        "day_of_week": row.day_of_week,
        "is_closed": bool(row.is_closed),
        "open_time": row.open_time.isoformat() if row.open_time else None,
        "close_time": row.close_time.isoformat() if row.close_time else None,
        "slot_interval_minutes": row.slot_interval_minutes,
    }


def _exception_row_json(row):
    return {
        "id": row.id,
        "exception_date": row.exception_date.isoformat(),
        "exception_type": row.exception_type.value
        if hasattr(row.exception_type, "value") else str(row.exception_type),
        "reason": row.reason,
        "opens_at": row.opens_at.isoformat() if row.opens_at else None,
        "closes_at": row.closes_at.isoformat() if row.closes_at else None,
        "capacity_factor": float(row.capacity_factor)
        if row.capacity_factor is not None else None,
    }


@router.get("/schedule", response_class=HTMLResponse)
async def schedule_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    weekly = await schedule_service.list_weekly_schedules(
        db, org.id,
        scope_level=ScheduleScopeLevel.ORGANIZATION, scope_id=org.id,
    )
    exceptions = await schedule_service.list_schedule_exceptions(
        db, org.id,
        scope_level=ScheduleScopeLevel.ORGANIZATION, scope_id=org.id,
    )
    weekly_json = [
        {**_weekly_row_json(r), "day_name": _SCHEDULE_DAYS[r.day_of_week]}
        for r in weekly
    ]
    exceptions_json = [_exception_row_json(r) for r in exceptions]
    return templates.TemplateResponse("org/schedule.html", {
        "request": request,
        "current_org": org,
        "active_page": "schedule",
        "weekly_json": weekly_json,
        "exceptions_json": exceptions_json,
        "schedule_days": _SCHEDULE_DAYS,
    })


def _optional_time(raw):
    """'HH:MM[:SS]' → time, or None for blank input."""
    if raw is None or str(raw).strip() == "":
        return None
    return dt_time.fromisoformat(str(raw).strip())


@router.post("/schedule/weekly/save")
async def schedule_weekly_save(request: Request, db: AsyncSession = Depends(get_db)):
    """Create or update ONE weekday row at organization scope (cookie session)."""
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    try:
        day_of_week = int(data.get("day_of_week"))
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 (Monday) and 6 (Sunday)")
        is_closed = bool(data.get("is_closed"))
        open_time = _optional_time(data.get("open_time"))
        close_time = _optional_time(data.get("close_time"))
        raw_interval = data.get("slot_interval_minutes")
        interval = int(raw_interval) if raw_interval not in (None, "") else None

        rows = await schedule_service.list_weekly_schedules(
            db, org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION, scope_id=org.id,
        )
        existing = next(
            (r for r in rows if r.day_of_week == day_of_week), None
        )
        if is_closed:
            payload = {"is_closed": True}
        else:
            payload = {
                "open_time": open_time,
                "close_time": close_time,
            }
            if interval is not None:
                payload["slot_interval_minutes"] = interval
        if existing is None:
            payload.update({
                "scope_level": ScheduleScopeLevel.ORGANIZATION.value,
                "scope_id": org.id,
                "day_of_week": day_of_week,
            })
            row = await schedule_service.create_weekly_schedule(db, org.id, payload)
        else:
            row = await schedule_service.update_weekly_schedule(
                db, org.id, existing.id, payload
            )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"data": _weekly_row_json(row)})


@router.post("/schedule/weekly/delete")
async def schedule_weekly_delete(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    try:
        day_of_week = int(data.get("day_of_week"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid weekday"}, status_code=400)
    rows = await schedule_service.list_weekly_schedules(
        db, org.id,
        scope_level=ScheduleScopeLevel.ORGANIZATION, scope_id=org.id,
    )
    existing = next((r for r in rows if r.day_of_week == day_of_week), None)
    if existing is None:
        return JSONResponse(
            {"error": "No schedule row published for this weekday"}, status_code=404
        )
    try:
        await schedule_service.delete_weekly_schedule(db, org.id, existing.id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"data": {"deleted": True}})


@router.post("/schedule/exception/create")
async def schedule_exception_create(request: Request, db: AsyncSession = Depends(get_db)):
    """Create one organization-scope schedule exception (holiday calendar)."""
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    try:
        payload = {
            "scope_level": ScheduleScopeLevel.ORGANIZATION.value,
            "scope_id": org.id,
            "exception_date": dt_date.fromisoformat(str(data["exception_date"])),
            "exception_type": str(data["exception_type"]),
            "reason": str(data.get("reason", "")).strip(),
        }
        if not payload["reason"]:
            raise ValueError("A user-facing reason is required for exceptions")
        opens_at = _optional_time(data.get("opens_at"))
        closes_at = _optional_time(data.get("closes_at"))
        raw_factor = data.get("capacity_factor")
        if opens_at is not None:
            payload["opens_at"] = opens_at
        if closes_at is not None:
            payload["closes_at"] = closes_at
        if raw_factor not in (None, ""):
            try:
                payload["capacity_factor"] = Decimal(str(raw_factor).strip())
            except InvalidOperation:
                raise ValueError("capacity_factor must be a decimal (0 < f <= 1)")
        row = await schedule_service.create_schedule_exception(db, org.id, payload)
    except (ValueError, KeyError) as e:
        return JSONResponse(
            {"error": str(e) if isinstance(e, ValueError) else "exception_date is required"},
            status_code=400,
        )
    return JSONResponse({"data": _exception_row_json(row)})


@router.post("/schedule/exception/{exception_id}/delete")
async def schedule_exception_delete(
    exception_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        await schedule_service.delete_schedule_exception(db, org.id, exception_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse({"data": {"deleted": True}})


# ── Services ──────────────────────────────────────────────────────────────────

@router.get("/services", response_class=HTMLResponse)
async def services_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    from app.services import catalogue_service
    services = await catalogue_service.get_service_catalogue(db, org.id, include_sub_services=True)
    branches = await branch_service.list_branches(db, org.id)
    services_json = [
        {
            "id": s.id,
            "name": s.name,
            "service_code": s.service_code or "",
            "branch_id": s.branch_id,
            "description": s.description or "",
            "price": float(s.price),
            "estimated_minutes": s.estimated_minutes,
            "is_active": s.is_active,
            "requires_identity_verification": s.requires_identity_verification,
            "sub_services": [
                {
                    "id": sub.id,
                    "name": sub.name,
                    "code": sub.code,
                    "additional_price": float(sub.additional_price),
                    "additional_minutes": sub.additional_minutes,
                    "is_active": sub.is_active,
                    "display_order": sub.display_order,
                }
                for sub in s.sub_services
            ],
        }
        for s in services
    ]
    return templates.TemplateResponse("org/services.html", {
        "request": request,
        "current_org": org,
        "active_page": "services",
        "services": services,
        "services_json": services_json,
        "branches": branches,
    })


# ── Phase 2: Dynamic Custom Forms ─────────────────────────────────────────────

@router.get("/forms", response_class=HTMLResponse)
async def forms_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    from app.services import form_service
    forms = await form_service.list_forms_by_org(db, org.id)
    services = await org_service.list_services(db, org.id)
    forms_json = [
        {
            "id": f.id,
            "title": f.title,
            "description": f.description or "",
            "service_id": f.service_id,
            "version": f.version,
            "is_published": f.is_published,
            "fields_count": len(f.fields),
        }
        for f in forms
    ]
    return templates.TemplateResponse("org/forms.html", {
        "request": request,
        "current_org": org,
        "active_page": "forms",
        "forms": forms,
        "forms_json": forms_json,
        "services": services,
    })


# ── Phase 2: KYC Identity Verification Review Desk ────────────────────────────

@router.get("/identities", response_class=HTMLResponse)
async def identities_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir

    from app.services import identity_service
    identities = await identity_service.list_identities_for_review(db, organization_id=org.id)
    return templates.TemplateResponse("org/identities.html", {
        "request": request,
        "current_org": org,
        "active_page": "identities",
        "identities": identities,
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

    # Phase 5: Derived Ledger totals, Cash Reconciliation, Payouts & Bank Account
    ledger_totals = await ledger_service.get_ledger_totals(db, org.id)
    ledger_entries, _ = await ledger_service.get_ledger_entries(db, org.id, per_page=15)
    cash_report = await cash_service.get_financial_breakdown_report(db, org.id)
    payouts, _ = await payout_service.list_payouts(db, org.id, per_page=10)
    bank_account = await bank_account_service.get_bank_account(db, org.id)

    return templates.TemplateResponse("org/payments.html", {
        "request": request,
        "current_org": org,
        "active_page": "payments",
        "payments": payments,
        "total": total,
        "total_revenue": total_revenue,
        "page": page,
        "pages": math.ceil(total / per_page) if total else 1,
        "ledger_totals": ledger_totals,
        "ledger_entries": ledger_entries,
        "cash_report": cash_report,
        "payouts": payouts,
        "bank_account": bank_account,
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


# ── Shifts & Kiosks HTML Pages ────────────────────────────────────────────────

@router.get("/shifts", response_class=HTMLResponse)
async def shifts_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir
    return templates.TemplateResponse(
        "org/shifts.html",
        {
            "request": request,
            "current_org": org,
            "active_page": "shifts",
        },
    )


@router.get("/kiosks", response_class=HTMLResponse)
async def kiosks_page(request: Request, db: AsyncSession = Depends(get_db)):
    org, redir = await _get_org_or_redirect(request, db)
    if redir:
        return redir
    branches = await branch_service.list_branches(db, org.id)
    return templates.TemplateResponse(
        "org/kiosks.html",
        {
            "request": request,
            "current_org": org,
            "active_page": "kiosks",
            "branches": branches,
        },
    )


# ── Logout ────────────────────────────────────────────────────────────────────

@router.get("/logout")
async def logout():
    resp = RedirectResponse("/org/login", status_code=302)
    resp.delete_cookie("org_token")
    return resp
