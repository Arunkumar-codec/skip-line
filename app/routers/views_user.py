"""
app/routers/views_user.py
──────────────────────────
Jinja2 HTML page routes for the user portal and public-facing pages.
"""

import math

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.common import PaginatedResponse
from app.schemas.token import TokenRead
from app.services import user_service, token_service
from app.utils.jwt import create_access_token, decode_token
from models.counter import Counter
from models.organization import Organization, OrgStatus
from models.service import Service
from models.token import Token
from models.user import User

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _redirect_login():
    return RedirectResponse("/user/login", status_code=302)


async def _get_user_or_redirect(request: Request, db: AsyncSession):
    token = request.cookies.get("user_token")
    if not token:
        return None, _redirect_login()
    payload = decode_token(token)
    if not payload or payload.get("role") != "user":
        return None, _redirect_login()
    user = await db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        return None, _redirect_login()
    return user, None


# ── Auth ──────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse("shared/login.html", {
        "request": request,
        "portal_name": "User",
        "login_action": "/user/login/submit",
        "register_url": "/user/register",
        "back_url": "/",
        "error": error,
    })


@router.post("/login/submit")
async def login_submit(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    user = await user_service.authenticate_user(db, form["email"], form["password"])
    if not user:
        return RedirectResponse("/user/login?error=Invalid+credentials", status_code=302)
    token = create_access_token(user.id, "user")
    resp = RedirectResponse("/user/dashboard", status_code=302)
    resp.set_cookie("user_token", token, httponly=True, samesite="lax", max_age=3600)
    return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = None):
    return templates.TemplateResponse("user/register.html", {
        "request": request,
        "error": error,
    })


@router.post("/register/submit")
async def register_submit(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    data = dict(form)
    try:
        await user_service.register_user(db, data)
        return RedirectResponse("/user/login", status_code=302)
    except ValueError as e:
        return RedirectResponse(f"/user/register?error={str(e)}", status_code=302)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir

    active = await user_service.get_user_active_tokens(db, user.id)
    return templates.TemplateResponse("user/dashboard.html", {
        "request": request,
        "current_user": user,
        "active_page": "dashboard",
        "active_tokens": active,
    })


# ── Organization search ───────────────────────────────────────────────────────

@router.get("/organizations", response_class=HTMLResponse)
async def organizations(
    request: Request,
    q: str = None,
    org_type: str = None,
    city: str = None,
    state: str = None,
    page: int = 1,
    db: AsyncSession = Depends(get_db),
):
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir

    orgs, total = await user_service.search_organizations(
        db, query=q, org_type=org_type, city=city, state=state, page=page
    )
    return templates.TemplateResponse("user/organizations.html", {
        "request": request,
        "current_user": user,
        "active_page": "search",
        "orgs": orgs,
        "total": total,
        "page": page,
        "pages": math.ceil(total / 20) if total else 1,
        "q": q,
        "type_filter": org_type,
        "city": city,         # FIX (ISSUE-D): template uses {{ city or '' }} and {{ state or '' }}
        "state": state,       # not city_filter/state_filter - filter values were lost on refresh
    })


# ── Token history ─────────────────────────────────────────────────────────────

@router.get("/tokens/history")
async def token_history(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    # API client content-negotiation: return JSON if Authorization header or Accept: application/json
    auth_header = request.headers.get("authorization")
    accept_header = request.headers.get("accept", "")
    if auth_header or "application/json" in accept_header:
        token = auth_header.replace("Bearer ", "").strip() if auth_header else request.cookies.get("user_token")
        if not token:
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        payload = decode_token(token)
        if not payload or payload.get("role") != "user":
            return JSONResponse(status_code=403, content={"detail": "User access required"})
        user = await db.get(User, int(payload["sub"]))
        if not user or not user.is_active:
            return JSONResponse(status_code=401, content={"detail": "User not found or inactive"})
        tokens, total = await user_service.get_user_token_history(db, user.id, page, per_page)
        return PaginatedResponse(
            total=total, page=page, per_page=per_page,
            pages=math.ceil(total / per_page) if total else 1,
            data=[TokenRead.model_validate(t) for t in tokens],
        )

    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir

    tokens, total = await user_service.get_user_token_history(db, user.id, page)
    return templates.TemplateResponse("user/token_history.html", {
        "request": request,
        "current_user": user,
        "active_page": "token_history",
        "tokens": tokens,
        "total": total,
        "page": page,
        "pages": math.ceil(total / 20) if total else 1,
    })


# ── Profile ───────────────────────────────────────────────────────────────────

@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    success: str = None,   # FIX (ISSUE-C): template shows success/error alerts
    error: str = None,
):
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir
    return templates.TemplateResponse("user/profile.html", {
        "request": request,
        "current_user": user,
        "active_page": "profile",
        "success": success,
        "error": error,
    })


@router.post("/profile")
async def update_profile(request: Request, db: AsyncSession = Depends(get_db)):
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir
    form = await request.form()
    await user_service.update_user(db, user, {
        "name": form.get("name"), "mobile": form.get("mobile"), "address": form.get("address")
    })
    return RedirectResponse("/user/profile?success=1", status_code=302)


# ── Notifications page ────────────────────────────────────────────────────────

@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request, db: AsyncSession = Depends(get_db)):
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir
    return templates.TemplateResponse("user/notifications.html", {
        "request": request,
        "current_user": user,
        "active_page": "notifications",
    })


# ── Logout ────────────────────────────────────────────────────────────────────

@router.get("/logout")
async def logout():
    resp = RedirectResponse("/user/login", status_code=302)
    resp.delete_cookie("user_token")
    return resp


# ── Public: Token tracking page ───────────────────────────────────────────────

@router.get("/track/{token_id}", response_class=HTMLResponse)
async def track_page(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        tracking = await token_service.get_token_tracking(db, token_id)
    except ValueError:
        return HTMLResponse("<h3>Token not found</h3>", status_code=404)

    token = await db.get(Token, token_id)
    org   = await db.get(Organization, token.organization_id) if token else None

    return templates.TemplateResponse("public/track.html", {
        "request": request,
        "token_id": token_id,
        "token_number": tracking["token_number"],
        "org_name": org.name if org else "Organization",
        "tracking": tracking,
    })


# ── Public: Kiosk page ────────────────────────────────────────────────────────

@router.get("/kiosk/{org_id}", response_class=HTMLResponse)
async def kiosk_page(org_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    org = await db.get(Organization, org_id)
    if not org or org.status != OrgStatus.APPROVED:
        return HTMLResponse("<h3>Organization not found</h3>", status_code=404)

    services = (await db.execute(
        select(Service).where(Service.organization_id == org_id, Service.is_active == True)  # noqa: E712
    )).scalars().all()

    from models.counter import CounterStatus
    counters = (await db.execute(
        select(Counter).where(
            Counter.organization_id == org_id, Counter.status == CounterStatus.ACTIVE
        ).order_by(Counter.counter_number)
    )).scalars().all()

    return templates.TemplateResponse("public/kiosk.html", {
        "request": request,
        "org_id": org_id,
        "org_name": org.name,
        "services": services,
        "counters": counters,
    })


# ── Active Tokens page ────────────────────────────────────────────────────────
# NEW ROUTE — was missing; user sidebar links to /user/tokens/active

@router.get("/tokens/active", response_class=HTMLResponse)
async def active_tokens_page(request: Request, db: AsyncSession = Depends(get_db)):
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir

    active = await user_service.get_user_active_tokens(db, user.id)
    return templates.TemplateResponse("user/dashboard.html", {
        "request": request,
        "current_user": user,
        "active_page": "active_tokens",
        "active_tokens": active,
    })


# ── Customer Identity Verification (KYC) page ─────────────────────────────────

@router.get("/identity", response_class=HTMLResponse)
async def user_identity_page(request: Request, db: AsyncSession = Depends(get_db)):
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir

    from app.services import identity_service
    records = await identity_service.list_user_identities(db, user.id)
    return templates.TemplateResponse("user/identity.html", {
        "request": request,
        "current_user": user,
        "active_page": "identity",
        "identities": records,
    })
