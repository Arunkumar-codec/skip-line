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
from app.config import settings
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
    return templates.TemplateResponse(request, "shared/login.html", {
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
    return templates.TemplateResponse(request, "user/register.html", {
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
    return templates.TemplateResponse(request, "user/dashboard.html", {
        "request": request,
        "current_user": user,
        "active_page": "dashboard",
        "active_tokens": active,
    })


# ── AI booking assistant (Phase 9, Workstreams C & D) ─────────────────────────

@router.get("/assistant", response_class=HTMLResponse)
async def assistant(
    request: Request,
    org: int = None,
    org_name: str = None,
    service: int = None,
    service_name: str = None,
    counter: int = None,
    counter_name: str = None,
    slot_id: int = None,
    slot_label: str = None,
    db: AsyncSession = Depends(get_db),
):
    """The AI chat page (C + D). Query params let the MANUAL booking form hand
    its current selections over as `manual_context` (D1) — the AI then
    continues from exactly where the user left off, and 'Switch to manual'
    (D2) hands the draft back the other way."""
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir

    manual_context = {k: v for k, v in {
        "organization_id": org, "organization_name": org_name,
        "service_id": service, "service_name": service_name,
        "counter_id": counter, "counter_name": counter_name,
        "slot_id": slot_id, "slot_label": slot_label,
    }.items() if v not in (None, "")}

    return templates.TemplateResponse(request, "user/assistant.html", {
        "request": request,
        "current_user": user,
        "active_page": "assistant",
        "manual_context": manual_context,
        "ai_available": bool(
            settings.ai_provider and settings.ai_base_url
            and settings.ai_api_key and settings.ai_model
        ),
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
    return templates.TemplateResponse(request, "user/organizations.html", {
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
    return templates.TemplateResponse(request, "user/token_history.html", {
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
    return templates.TemplateResponse(request, "user/profile.html", {
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
    return templates.TemplateResponse(request, "user/notifications.html", {
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


# ── Token tracking page (user portal) ────────────────────────────────────────
# Phase 9 Workstream A2 (HIGH-finding class fix): this endpoint previously
# rendered ANY token's tracking page from the bare numeric id with no owner
# check. Now: requires the user session and enforces token ownership — a
# non-owned, guest (user_id NULL) or nonexistent id all return the SAME
# uninformative 404, so probing numeric ids never reveals token existence.
# Guest tracking remains available via the purpose-bound signed reference at
# GET /public/track/{ref} (QR-002), which is unchanged.

@router.get("/track/{token_id}", response_class=HTMLResponse)
async def track_page(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir

    token = await db.get(Token, token_id)
    if not token or token.user_id != user.id:
        return HTMLResponse("<h3>Token not found</h3>", status_code=404)

    try:
        tracking = await token_service.get_token_tracking(db, token_id)
    except ValueError:
        return HTMLResponse("<h3>Token not found</h3>", status_code=404)

    org = await db.get(Organization, token.organization_id)

    from app.utils import signed_qr

    return templates.TemplateResponse(request, "public/track.html", {
        "request": request,
        "token_id": token_id,
        "ws_ref": signed_qr.create_token_ref(
            token_id, token.organization_id, signed_qr.PURPOSE_TRACK
        ),
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

    return templates.TemplateResponse(request, "public/kiosk.html", {
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
    return templates.TemplateResponse(request, "user/dashboard.html", {
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
    return templates.TemplateResponse(request, "user/identity.html", {
        "request": request,
        "current_user": user,
        "active_page": "identity",
        "identities": records,
    })

# -- Phase 7: Verified Feedback (My Ratings) ----------------------------------

@router.get("/feedback", response_class=HTMLResponse)
async def user_feedback_page(request: Request, db: AsyncSession = Depends(get_db)):
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir

    from sqlalchemy import select
    from models.feedback import Feedback
    from models.token import Token, TokenStatus

    rows = (
        await db.execute(
            select(Feedback).where(Feedback.user_id == user.id)
            .order_by(Feedback.id.desc())
        )
    ).scalars().all()
    rated_ids = {fb.token_id for fb in rows}
    tokens = (
        await db.execute(
            select(Token).where(
                Token.user_id == user.id,
                Token.status == TokenStatus.COMPLETED,
            ).order_by(Token.id.desc())
        )
    ).scalars().all()
    rateable = [t for t in tokens if t.id not in rated_ids]
    return templates.TemplateResponse(request, "user/feedback.html", {
        "request": request,
        "current_user": user,
        "active_page": "feedback",
        "ratings": rows,
        "rateable": rateable,
    })


# -- Phase 7: Unified Support (My Cases) --------------------------------------

@router.get("/cases", response_class=HTMLResponse)
async def user_cases_page(request: Request, db: AsyncSession = Depends(get_db)):
    user, redir = await _get_user_or_redirect(request, db)
    if redir:
        return redir

    from sqlalchemy import select
    from models.organization import Organization, OrgStatus
    from models.support import CaseParticipant, SupportCase

    ids = (
        await db.execute(
            select(CaseParticipant.case_id).where(
                CaseParticipant.participant_type == "user",
                CaseParticipant.participant_id == user.id,
            )
        )
    ).scalars().all()
    cases = list(
        (
            await db.execute(
                select(SupportCase).where(SupportCase.id.in_(ids))
                .order_by(SupportCase.id.desc())
            )
        ).scalars().all()
    ) if ids else []
    orgs = list(
        (
            await db.execute(
                select(Organization).where(
                    Organization.status == OrgStatus.APPROVED
                ).order_by(Organization.name)
            )
        ).scalars().all()
    )
    return templates.TemplateResponse(request, "user/cases.html", {
        "request": request,
        "current_user": user,
        "active_page": "support",
        "cases": cases,
        "organizations": orgs,
    })
