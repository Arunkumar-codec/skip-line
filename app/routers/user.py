"""
app/routers/user.py
────────────────────
User REST API endpoints (JSON).
GET routes that conflicted with views_user.py HTML routes have been renamed
to /api/* so that the HTML view router always wins the first-match.
"""

import math
from typing import Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.schemas.auth import ChangePasswordRequest
from app.schemas.common import APIResponse, MessageResponse, PaginatedResponse
from app.schemas.identity import IdentitySubmitRequest
from app.schemas.organization import OrgSearchResult
from app.schemas.token import TokenBookRequest, TokenRead
from app.schemas.user import UserRead, UserRegisterRequest, UserUpdateRequest
from app.services import user_service, token_service, notification_service
from models.user import User

router = APIRouter()


# ── Registration (public) ─────────────────────────────────────────────────────

@router.post("/register", response_model=APIResponse)
async def register(body: UserRegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        user = await user_service.register_user(db, body.model_dump())
        return APIResponse(message="Registration successful", data=UserRead.model_validate(user))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Dashboard / profile (JSON API) ───────────────────────────────────────────
# Renamed from /dashboard and /profile to /api/* to avoid shadowing HTML views.

@router.get("/api/dashboard", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def dashboard_api(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    active = await user_service.get_user_active_tokens(db, user.id)
    return APIResponse(data={
        "user": UserRead.model_validate(user),
        "active_tokens": [TokenRead.model_validate(t) for t in active],
        "active_count": len(active),
    })


@router.get("/api/profile", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def profile_api(user: User = Depends(get_current_user)):
    return APIResponse(data=UserRead.model_validate(user))


@router.put("/profile", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def update_profile(
    body: UserUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    updated = await user_service.update_user(db, user, body.model_dump(exclude_none=True))
    return APIResponse(data=UserRead.model_validate(updated))


@router.post("/change-password", response_model=MessageResponse, dependencies=[Depends(get_current_user)])
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    ok = await user_service.change_user_password(db, user, body.current_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail="Current password incorrect")
    return MessageResponse(message="Password changed")


# ── Organization search ───────────────────────────────────────────────────────

@router.get("/organizations/search", response_model=PaginatedResponse[OrgSearchResult])
async def search_orgs(
    q: str = None,
    org_type: str = None,
    city: str = None,
    state: str = None,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    orgs, total = await user_service.search_organizations(
        db, query=q, org_type=org_type, city=city, state=state, page=page, per_page=per_page
    )
    return PaginatedResponse(
        total=total, page=page, per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[OrgSearchResult.model_validate(o) for o in orgs],
    )


# ── Token booking ─────────────────────────────────────────────────────────────

@router.post("/tokens/book", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def book_token(
    body: TokenBookRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        token = await token_service.book_token(
            db,
            org_id=body.organization_id,
            counter_id=body.counter_id,
            service_id=body.service_id,
            user_id=user.id,
            sub_service_ids=body.sub_service_ids,
            form_answers=body.form_answers,
        )
        await notification_service.notify_token_created(db, token, user)
        await db.commit()
        return APIResponse(message="Token booked", data=TokenRead.model_validate(token))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Token history (JSON API) ──────────────────────────────────────────────────
# Renamed from /tokens/history to /api/tokens/history to avoid shadowing HTML view.

@router.get("/api/tokens/history", response_model=PaginatedResponse[TokenRead], dependencies=[Depends(get_current_user)])
async def token_history_api(
    page: int = 1,
    per_page: int = 20,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tokens, total = await user_service.get_user_token_history(db, user.id, page, per_page)
    return PaginatedResponse(
        total=total, page=page, per_page=per_page,
        pages=math.ceil(total / per_page) if total else 1,
        data=[TokenRead.model_validate(t) for t in tokens],
    )


# ── Live tracking ─────────────────────────────────────────────────────────────

@router.get("/tokens/{token_id}/track", response_model=APIResponse)
@router.get("/api/tokens/{token_id}/track", response_model=APIResponse)
async def track_token(token_id: int, db: AsyncSession = Depends(get_db)):
    try:
        data = await token_service.get_token_tracking(db, token_id)
        return APIResponse(data=data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Customer Identity Verification (KYC) ─────────────────────────────────────

@router.post("/api/identity/submit", response_model=APIResponse, dependencies=[Depends(get_current_user)])
@router.post("/identity/submit", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def submit_identity(
    id_type: str = Form(...),
    id_number: str = Form(...),
    organization_id: Optional[int] = Form(None),
    document: Optional[UploadFile] = File(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import identity_service
    from app.schemas.identity import UserIdentityRead

    doc_bytes = None
    doc_filename = None
    if document and document.filename:
        doc_bytes = await document.read()
        doc_filename = document.filename

    identity = await identity_service.submit_identity(
        db=db,
        user_id=user.id,
        id_type=id_type,
        raw_id_number=id_number,
        organization_id=organization_id,
        document_bytes=doc_bytes,
        document_filename=doc_filename,
    )
    return APIResponse(
        message="Identity submitted for verification",
        data=UserIdentityRead.model_validate(identity),
    )


@router.post("/api/identity/submit-json", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def submit_identity_json(
    body: IdentitySubmitRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import identity_service
    from app.schemas.identity import UserIdentityRead

    identity = await identity_service.submit_identity(
        db=db,
        user_id=user.id,
        id_type=body.id_type,
        raw_id_number=body.id_number,
        organization_id=body.organization_id,
    )
    return APIResponse(
        message="Identity submitted for verification",
        data=UserIdentityRead.model_validate(identity),
    )


@router.get("/api/identity", response_model=APIResponse, dependencies=[Depends(get_current_user)])
@router.get("/identity/list", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def get_my_identities(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services import identity_service
    from app.schemas.identity import UserIdentityRead

    records = await identity_service.list_user_identities(db, user.id)
    return APIResponse(data=[UserIdentityRead.model_validate(r) for r in records])


@router.get("/identity/{identity_id}/document", dependencies=[Depends(get_current_user)])
async def get_my_identity_document(
    identity_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from pathlib import Path
    from fastapi.responses import FileResponse
    from models.user_identity import UserIdentity

    identity = await db.get(UserIdentity, identity_id)
    if not identity or identity.user_id != user.id:
        raise HTTPException(status_code=404, detail="Identity record not found")
    if not identity.document_path or not Path(identity.document_path).exists():
        raise HTTPException(status_code=404, detail="Document file not found")

    ext = Path(identity.document_path).suffix.lower()
    media_type = "application/pdf" if ext == ".pdf" else f"image/{ext.replace('.', '')}"
    return FileResponse(identity.document_path, media_type=media_type)


# ── Catalogue & Forms for Booking ─────────────────────────────────────────────

@router.get("/api/catalogue/{org_id}", response_model=APIResponse)
async def get_org_catalogue(
    org_id: int,
    branch_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    from app.services import catalogue_service
    from app.schemas.catalogue import ServiceDetailRead

    services = await catalogue_service.get_service_catalogue(
        db, organization_id=org_id, branch_id=branch_id, include_sub_services=True
    )
    return APIResponse(data=[ServiceDetailRead.model_validate(s) for s in services])


@router.get("/api/forms/service/{service_id}", response_model=APIResponse)
async def get_service_active_form(
    service_id: int,
    db: AsyncSession = Depends(get_db),
):
    from app.services import form_service
    from app.schemas.form import CustomFormRead

    form = await form_service.get_active_form_for_service(db, service_id)
    if not form:
        return APIResponse(data=None)
    return APIResponse(data=CustomFormRead.model_validate(form))


@router.get("/api/tokens/{token_id}/form", response_model=APIResponse, dependencies=[Depends(get_current_user)])
async def get_token_form_details(
    token_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select
    from models.token import Token
    from models.custom_form import TokenFormSnapshot
    from app.schemas.form import TokenFormSnapshotRead

    stmt = select(Token).where(Token.id == token_id, Token.user_id == user.id)
    result = await db.execute(stmt)
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    snap_stmt = (
        select(TokenFormSnapshot)
        .where(TokenFormSnapshot.token_id == token_id)
        .options(selectinload(TokenFormSnapshot.answers))
    )
    snap_res = await db.execute(snap_stmt)
    snapshot = snap_res.scalar_one_or_none()
    if not snapshot:
        return APIResponse(data=None)

    return APIResponse(data=TokenFormSnapshotRead.model_validate(snapshot))
