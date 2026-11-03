"""
app/routers/trust.py
───────────────────
Phase 7 — Trust surface: verified feedback (FDB), unified support cases
(SUP/CASE), knowledge solutions (KB) and organization KYC (ORGKYC).

Every endpoint authenticates server-side (/user/* → get_current_user,
/org/* → get_current_org, /admin/* → get_current_admin) and delegates
all authorization rules (ownership, tenancy, participant membership,
state transitions) to the corresponding services. Attachment and KYC
document downloads are member/owner-gated AND access-logged.
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_admin, get_current_org, get_current_user
from app.schemas.common import APIResponse
from app.schemas import trust as ts
from app.services import (
    feedback_service,
    knowledge_service,
    kyc_service,
    support_service,
    attachment_service,
)
from app.services.audit_service import log_audit_event
from models.admin import Admin
from models.organization import Organization
from models.user import User

router = APIRouter()


# ════════════════════ VERIFIED FEEDBACK (FDB-001..005) ════════════════════

@router.post("/user/api/tokens/{token_id}/feedback", response_model=APIResponse)
async def submit_feedback(
    token_id: int,
    body: ts.FeedbackSubmit,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        feedback = await feedback_service.submit_feedback(
            db, token_id, user.id,
            rating_overall=body.rating_overall,
            rating_waiting=body.rating_waiting,
            rating_staff=body.rating_staff,
            rating_service=body.rating_service,
            result=body.result,
            comment=body.comment,
            is_anonymous=body.is_anonymous,
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return APIResponse(
        message="Feedback submitted for moderation", data={"id": feedback.id}
    )


@router.get("/user/api/tokens/{token_id}/feedback", response_model=APIResponse)
async def my_token_feedback(
    token_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from models.token import Token

    token = await db.get(Token, token_id)
    if token is None or token.user_id != user.id:
        raise HTTPException(404, "Token not found")
    feedback = await feedback_service.get_my_feedback_for_token(db, token_id)
    return APIResponse(data={"id": feedback.id if feedback else None})


@router.get("/public/api/organizations/{org_id}/reviews", response_model=APIResponse)
async def public_reviews(org_id: int, db: AsyncSession = Depends(get_db)):
    from models.organization import Organization

    org = await db.get(Organization, org_id)
    if org is None or getattr(org, "status", None) != "approved":
        raise HTTPException(404, "Organization not found")
    return APIResponse(
        data=await feedback_service.list_public_reviews(db, org_id)
    )


@router.get("/public/api/organizations/{org_id}/reviews/summary", response_model=APIResponse)
async def public_reviews_summary(org_id: int, db: AsyncSession = Depends(get_db)):
    return APIResponse(data=await feedback_service.public_summary(db, org_id))


@router.get("/org/api/feedback", response_model=APIResponse)
async def org_feedback_list(
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from app.services.feedback_service import list_public_reviews, public_summary
    # The org desk sees its moderation pipeline (pending/report/hidden),
    # public reviews give the published view.
    from sqlalchemy import select
    from models.feedback import Feedback

    rows = (
        await db.execute(
            select(Feedback)
            .where(Feedback.organization_id == org.id)
            .order_by(Feedback.id.desc())
        )
    ).scalars().all()
    data = {
        "reviews": await list_public_reviews(db, org.id),
        "summary": await public_summary(db, org.id),
        "moderation": [
            {
                "id": fb.id, "rating": fb.rating_overall,
                "comment": fb.comment,
                "status": fb.moderation_status.value
                if hasattr(fb.moderation_status, "value") else fb.moderation_status,
            }
            for fb in rows
        ],
    }
    return APIResponse(data=data)


@router.post("/org/api/feedback/{feedback_id}/respond", response_model=APIResponse)
async def org_respond(
    feedback_id: int,
    body: ts.FeedbackResponseIn,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        response = await feedback_service.org_respond(db, feedback_id, org.id, body.body)
    except LookupError as e:
        raise HTTPException(404, str(e))
    return APIResponse(
        message="Response published", data={"response_id": response.id}
    )


@router.post("/org/api/feedback/{feedback_id}/report", response_model=APIResponse)
async def org_report_feedback(
    feedback_id: int,
    body: ts.FeedbackResponseIn,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        await feedback_service.org_report(db, feedback_id, org.id, body.body)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    await log_audit_event(
        db, action="feedback_reported_by_org", actor_type="organization",
        actor_id=org.id, organization_id=org.id,
        target_type="feedback", target_id=feedback_id,
    )
    return APIResponse(message="Feedback reported to moderation")


@router.get("/admin/api/feedback", response_model=APIResponse)
async def admin_feedback_moderation_list(
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select
    from models.feedback import Feedback, ModerationStatus

    rows = (
        await db.execute(
            select(Feedback)
            .where(Feedback.moderation_status.in_([
                ModerationStatus.PENDING, ModerationStatus.REPORTED,
                ModerationStatus.HIDDEN,
            ]))
            .order_by(Feedback.id.desc())
        )
    ).scalars().all()
    return APIResponse(data=[
        {
            "id": fb.id, "organization_id": fb.organization_id,
            "rating": fb.rating_overall, "comment": fb.comment,
            "status": fb.moderation_status.value,
        }
        for fb in rows
    ])


@router.post("/admin/api/feedback/{feedback_id}/moderate", response_model=APIResponse)
async def admin_moderate_feedback(
    feedback_id: int,
    body: ts.FeedbackModerateIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        feedback = await feedback_service.moderate(
            db, feedback_id, admin.id, body.action, body.reason
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return APIResponse(message="Moderated", data={"status": feedback.moderation_status.value})


# ════════════════════ UNIFIED SUPPORT (SUP/CASE) ═══════════════════════════

async def _case_detail(db: AsyncSession, case: support_service.SupportCase, viewer: str):
    messages = support_service._visible_messages(case, viewer)
    return ts.CaseDetailResponse(
        case=ts.CaseRead.model_validate(case),
        participants=[ts.CaseParticipantRead.model_validate(p) for p in case.participants],
        messages=[ts.CaseMessageRead.model_validate(m) for m in messages],
        attachments=[
            ts.AttachmentRead.model_validate(a)
            for a in case.attachments
            if viewer == "admin" or a.message_id is None
            or next((m for m in case.messages if m.id == a.message_id), None)
        ],
    )


@router.post("/user/api/cases", response_model=APIResponse)
async def user_create_case(
    body: ts.CaseCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        case = await support_service.create_case(
            db, "user", user.id, user.name,
            case_type=body.case_type, category=body.category,
            subject=body.subject, detail=body.detail,
            token_id=body.token_id, payment_id=body.payment_id,
            feedback_id=body.feedback_id, branch_id=body.branch_id,
            user_id=body.user_id, organization_id=body.organization_id,
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (ValueError, PermissionError) as e:
        raise HTTPException(403, str(e))
    return APIResponse(
        message="Case submitted", data={"id": case.id, "reference": case.reference}
    )


@router.post("/org/api/cases", response_model=APIResponse)
async def org_create_case(
    body: ts.CaseCreate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    try:
        case = await support_service.create_case(
            db, "organization", org.id, org.name or f"Organization#{org.id}",
            case_type=body.case_type, category=body.category,
            subject=body.subject, detail=body.detail,
            token_id=body.token_id, payment_id=body.payment_id,
            feedback_id=body.feedback_id, branch_id=body.branch_id,
            user_id=body.user_id, organization_id=body.organization_id,
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (ValueError, PermissionError) as e:
        raise HTTPException(403, str(e))
    return APIResponse(
        message="Case submitted", data={"id": case.id, "reference": case.reference}
    )


@router.get("/user/api/cases", response_model=APIResponse)
async def user_case_list(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    cases = await support_service.list_cases_for(db, "user", user.id)
    return APIResponse(data=[ts.CaseRead.model_validate(c) for c in cases])


@router.get("/org/api/cases", response_model=APIResponse)
async def org_case_list(
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)
):
    cases = await support_service.list_cases_for(db, "organization", org.id)
    return APIResponse(data=[ts.CaseRead.model_validate(c) for c in cases])


@router.get("/admin/api/cases", response_model=APIResponse)
async def admin_case_list(
    status: Optional[str] = None,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    cases = await support_service.list_cases_for(db, "admin", None, status=status)
    return APIResponse(data=[ts.CaseRead.model_validate(c) for c in cases])


async def _get_case(db, case_id, ptype, pid):
    try:
        return await support_service.get_case_for_participant(db, case_id, ptype, pid)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except PermissionError:
        raise HTTPException(403, "You are not a participant of this case")


@router.get("/user/api/cases/{case_id}", response_model=APIResponse)
async def user_case_detail(
    case_id: int, user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "user", user.id)
    return APIResponse(data=ts.CaseDetailResponse.model_validate(
        await _case_detail(db, case, "user")
    ))


@router.get("/org/api/cases/{case_id}", response_model=APIResponse)
async def org_case_detail(
    case_id: int, org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "organization", org.id)
    return APIResponse(data=ts.CaseDetailResponse.model_validate(
        await _case_detail(db, case, "organization")
    ))


@router.get("/admin/api/cases/{case_id}", response_model=APIResponse)
async def admin_case_detail(
    case_id: int, admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "admin", None)
    await support_service.admin_review_participants(db, case)
    return APIResponse(data=ts.CaseDetailResponse.model_validate(
        await _case_detail(db, case, "admin")
    ))


@router.post("/user/api/cases/{case_id}/messages", response_model=APIResponse)
async def user_case_message(
    case_id: int, body: ts.CaseMessageIn,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "user", user.id)
    try:
        message = await support_service.add_message(
            db, case, "user", user.id, user.name, body.body
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))
    return APIResponse(message="Message sent", data={"message_id": message.id})


@router.post("/org/api/cases/{case_id}/messages", response_model=APIResponse)
async def org_case_message(
    case_id: int, body: ts.CaseMessageIn,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "organization", org.id)
    try:
        message = await support_service.add_message(
            db, case, "organization", org.id,
            org.name or f"Organization#{org.id}", body.body
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))
    return APIResponse(message="Message sent", data={"message_id": message.id})


@router.post("/admin/api/cases/{case_id}/messages", response_model=APIResponse)
async def admin_case_message(
    case_id: int, body: ts.CaseMessageIn,
    internal: bool = False,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "admin", None)
    try:
        message = await support_service.add_message(
            db, case, "admin", admin.id, f"Admin #{admin.id}", body.body,
            visibility=(
                support_service.MessageVisibility.INTERNAL if internal
                else support_service.MessageVisibility.PARTIES
            ),
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))
    return APIResponse(message="Message sent", data={"message_id": message.id})


@router.post("/admin/api/cases/{case_id}/transition", response_model=APIResponse)
async def admin_case_transition(
    case_id: int, body: ts.CaseTransitionIn,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "admin", None)
    action_map = {
        "acknowledge": "acknowledged", "assign": "assigned",
        "start_review": "in_review", "wait_for_party": "waiting_for_party",
        "escalate": "escalated", "close": "closed",
        "propose_solution": "solution_proposed",
        "resolve": "resolved",
    }
    new_status = action_map.get(body.action)
    if new_status is None:
        raise HTTPException(422, f"Unknown transition '{body.action}'")
    try:
        case = await support_service.transition_case(
            db, case, "admin", new_status, note=body.note,
            assigned_admin_id=admin.id if new_status == "assigned" else None,
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))
    return APIResponse(message="Case updated", data={"status": case.status})


@router.post("/user/api/cases/{case_id}/resolution", response_model=APIResponse)
async def user_case_resolution(
    case_id: int, accept: bool,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "user", user.id)
    try:
        case = await support_service.customer_resolution_decision(
            db, case, accept, user.id, user.name
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))
    return APIResponse(message="Resolution recorded", data={"status": case.status})


@router.post("/user/api/cases/{case_id}/reopen", response_model=APIResponse)
async def user_case_reopen(
    case_id: int, body: ts.CaseMessageIn,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "user", user.id)
    try:
        case = await support_service.customer_reopen(db, case, user.id, body.body)
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))
    return APIResponse(message="Case reopened", data={"status": case.status})


# Real implementations for uploads/downloads (separate explicit routes so
# the auth dependencies and audit trail stay unambiguous).


@router.post("/user/api/cases/{case_id}/attachments", response_model=APIResponse)
async def user_case_upload(
    case_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "user", user.id)
    data = await file.read()
    path, name, size, mime, sha = attachment_service.save_private_file(
        "cases", data, file.filename
    )
    try:
        attachment = await support_service.add_attachment(
            db, case, path, name, mime, size, sha, "user", user.id
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))
    return APIResponse(
        message="Attachment uploaded",
        data={"attachment_id": attachment.id, "filename": name},
    )


@router.post("/org/api/cases/{case_id}/attachments", response_model=APIResponse)
async def org_case_upload(
    case_id: int,
    file: UploadFile = File(...),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "organization", org.id)
    data = await file.read()
    path, name, size, mime, sha = attachment_service.save_private_file(
        "cases", data, file.filename
    )
    try:
        attachment = await support_service.add_attachment(
            db, case, path, name, mime, size, sha, "organization", org.id
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))
    return APIResponse(
        message="Attachment uploaded",
        data={"attachment_id": attachment.id, "filename": name},
    )


@router.post("/admin/api/cases/{case_id}/attachments", response_model=APIResponse)
async def admin_case_upload(
    case_id: int,
    file: UploadFile = File(...),
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "admin", None)
    data = await file.read()
    path, name, size, mime, sha = attachment_service.save_private_file(
        "cases", data, file.filename
    )
    try:
        attachment = await support_service.add_attachment(
            db, case, path, name, mime, size, sha, "admin", admin.id
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))
    return APIResponse(
        message="Attachment uploaded",
        data={"attachment_id": attachment.id, "filename": name},
    )


async def _download_attachment(db, case_id, attachment_id, ptype, pid, ip=None):
    try:
        case = await support_service.get_case_for_participant(
            db, case_id, ptype, pid
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    except PermissionError:
        raise HTTPException(403, "You are not a participant of this case")
    try:
        attachment = await support_service.access_attachment(
            db, case, attachment_id, ptype, pid, ip_address=ip
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    path = Path(attachment.stored_path)
    if not path.exists():
        raise HTTPException(404, "Attachment file missing")
    from datetime import datetime, timezone as _tz
    return FileResponse(path, filename=attachment.filename,
                        media_type=attachment.mime or "application/octet-stream")


@router.get("/user/api/cases/{case_id}/attachments/{attachment_id}")
async def user_attachment_download(
    case_id: int, attachment_id: int,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    return await _download_attachment(db, case_id, attachment_id, "user", user.id)


@router.get("/org/api/cases/{case_id}/attachments/{attachment_id}")
async def org_attachment_download(
    case_id: int, attachment_id: int,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    return await _download_attachment(db, case_id, attachment_id, "organization", org.id)


@router.get("/admin/api/cases/{case_id}/attachments/{attachment_id}")
async def admin_attachment_download(
    case_id: int, attachment_id: int,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    return await _download_attachment(db, case_id, attachment_id, "admin", None)


# ── KB-002: suggested solutions inside cases ──────────────────────────────

@router.post("/org/api/cases/{case_id}/suggest", response_model=APIResponse)
async def org_suggest_solution(
    case_id: int, body: ts.CaseSuggestIn,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    case = await _get_case(db, case_id, "organization", org.id)
    from models.knowledge import KnowledgeSolution
    solution = await db.get(KnowledgeSolution, body.knowledge_solution_id)
    if solution is None or not solution.is_active:
        raise HTTPException(404, "Knowledge solution not found")
    if solution.organization_id not in (None, org.id):
        raise HTTPException(403, "Solution belongs to another organization")
    text = body.body_override or solution.body
    try:
        message, state = await support_service.propose_solution(
            db, case, text, is_sensitive=solution.sensitive,
            suggested_solution_id=solution.id,
            actor_type="organization", actor_id=org.id,
            actor_name=org.name or f"Organization#{org.id}",
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))
    return APIResponse(message="Solution proposed", data={
        "message_id": message.id, "status": state,
        "pending_review": message.review_status == "pending_review",
    })


@router.post("/admin/api/case-messages/{message_id}/approve", response_model=APIResponse)
async def admin_approve_suggested(
    message_id: int,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    """Human review gate for sensitive suggested solutions (KB-002)."""
    from models.support import CaseMessage, MessageReviewStatus
    from sqlalchemy import select

    message = (
        await db.execute(
            select(CaseMessage).where(CaseMessage.id == message_id)
        )
    ).scalar_one_or_none()
    if message is None:
        raise HTTPException(404, "Message not found")
    if message.review_status != MessageReviewStatus.PENDING_REVIEW:
        raise HTTPException(409, "Message is not awaiting review")
    message.review_status = MessageReviewStatus.APPROVED
    await db.commit()
    await log_audit_event(
        db, action="case_message_review_approved", actor_type="admin",
        actor_id=admin.id, target_type="case_message", target_id=message.id,
    )
    return APIResponse(message="Suggested solution approved and visible")


# ════════════════════ KNOWLEDGE SOLUTIONS (KB-001..002) ═══════════════════

@router.post("/org/api/knowledge", response_model=APIResponse)
async def org_create_knowledge(
    body: ts.KnowledgeIn,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    solution = await knowledge_service.create_solution(
        db, org.id, "organization", org.id, body.category, body.title,
        body.body, body.audience, body.sensitive,
    )
    return APIResponse(message="Template saved", data={"id": solution.id})


@router.get("/org/api/knowledge", response_model=APIResponse)
async def org_knowledge_list(
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)
):
    solutions = await knowledge_service.list_solutions(db, org.id)
    return APIResponse(data=[ts.KnowledgeRead.model_validate(s) for s in solutions])


@router.post("/org/api/knowledge/{solution_id}/deactivate", response_model=APIResponse)
async def org_deactivate_knowledge(
    solution_id: int,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    try:
        await knowledge_service.deactivate_solution(
            db, solution_id, "organization", org.id, organization_id=org.id
        )
    except LookupError:
        raise HTTPException(404, "Solution not found")
    except PermissionError:
        raise HTTPException(403, "You can only deactivate your own templates")
    return APIResponse(message="Template deactivated")


@router.post("/admin/api/knowledge", response_model=APIResponse)
async def admin_create_knowledge(
    body: ts.KnowledgeIn,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    solution = await knowledge_service.create_solution(
        db, None, "admin", admin.id, body.category, body.title,
        body.body, body.audience, body.sensitive, is_platform=True,
    )
    return APIResponse(message="Global template saved", data={"id": solution.id})


@router.get("/admin/api/knowledge", response_model=APIResponse)
async def admin_knowledge_list(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    solutions = await knowledge_service.list_solutions(db, None, platform_admin=True)
    return APIResponse(data=[ts.KnowledgeRead.model_validate(s) for s in solutions])


# ════════════════════ ORGANIZATION KYC (ORGKYC-001..007) ═══════════════════

@router.post("/org/api/kyc/application", response_model=APIResponse)
async def org_kyc_application(
    body: ts.KycApplicationIn,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    try:
        kyc = await kyc_service.save_application(
            db, org.id, body.model_dump()
        )
    except (ValueError, PermissionError) as e:
        status = 409 if isinstance(e, ValueError) else 403
        raise HTTPException(status, str(e))
    return APIResponse(message="Application data saved", data={"status": kyc.status})


@router.post("/org/api/kyc/documents", response_model=APIResponse)
async def org_kyc_document(
    document_type: str = Form(...),
    document_number: str = Form(...),
    expires_at: Optional[str] = Form(None),
    document: UploadFile = File(...),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime as _dt

    data = await document.read()
    try:
        path, name, size, mime, sha = attachment_service.save_private_file(
            f"kyc/{org.id}", data, document.filename
        )
    except HTTPException:
        raise
    expires = None
    if expires_at:
        try:
            expires = _dt.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, "Invalid expiry date")
    doc = await kyc_service.upload_document(
        db, org.id, document_type, document_number.strip(), path, expires
    )
    return APIResponse(
        message="Document uploaded",
        data={"document_id": doc.id, "filename": name},
    )


@router.post("/org/api/kyc/submit", response_model=APIResponse)
async def org_kyc_submit(
    body: ts.KycSubmitIn,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    try:
        kyc = await kyc_service.submit_application(db, org.id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (ValueError, PermissionError) as e:
        status = 403 if isinstance(e, PermissionError) else 409
        raise HTTPException(status, str(e))
    return APIResponse(message="Application submitted", data={"status": kyc.status})


@router.get("/org/api/kyc", response_model=APIResponse)
async def org_kyc_status(
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)
):
    kyc = await kyc_service.get_or_create_kyc(db, org.id)
    docs = await kyc_service.list_documents(db, org.id)
    requirements = await kyc_service.get_doc_requirements(
        db, kyc.org_category or org.org_type, org.id
    )
    data = ts.KycRead(
        id=kyc.id, organization_id=kyc.organization_id, status=kyc.status,
        tier=kyc.tier, legal_name=kyc.legal_name, org_category=kyc.org_category,
        registered_address=kyc.registered_address,
        contact_email=kyc.contact_email, contact_phone=kyc.contact_phone,
        representative_name=kyc.representative_name,
        representative_role=kyc.representative_role,
        representative_verified=kyc.representative_verified,
        rejection_reason=kyc.rejection_reason,
        more_info_request=kyc.more_info_request, expires_at=kyc.expires_at,
        appeal_status=kyc.appeal_status, reviewed_at=kyc.reviewed_at,
        history=kyc.history,
    )
    return APIResponse(data={
        "kyc": data.model_dump(),
        "documents": docs,
        "required_documents": sorted(requirements),
    })


@router.post("/org/api/kyc/appeal", response_model=APIResponse)
async def org_kyc_appeal(
    body: ts.KycAppealIn,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    try:
        kyc = await kyc_service.file_appeal(db, org.id, body.reason)
    except (ValueError, PermissionError) as e:
        status = 409 if isinstance(e, ValueError) else 403
        raise HTTPException(status, str(e))
    return APIResponse(message="Appeal filed", data={"appeal_status": kyc.appeal_status})


@router.get("/org/api/kyc/documents/{document_id}")
async def org_kyc_document_download(
    document_id: int,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    try:
        doc = await kyc_service.access_document(
            db, org.id, document_id, "organization", org.id
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    if not doc.document_url or not Path(doc.document_url).exists():
        raise HTTPException(404, "Document file not found")
    return FileResponse(doc.document_url, media_type="application/octet-stream")


# ── admin desk ─────────────────────────────────────────────────────────────

@router.get("/admin/api/kyc/queue", response_model=APIResponse)
async def admin_kyc_queue(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    return APIResponse(data=await kyc_service.review_queue(db))


@router.get("/admin/api/kyc/{organization_id}", response_model=APIResponse)
async def admin_kyc_detail(
    organization_id: int,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    from models.branch import Branch

    org = await db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(404, "Organization not found")
    kyc = await kyc_service.get_or_create_kyc(db, organization_id)
    docs = await kyc_service.list_documents(db, organization_id)
    indicators = await kyc_service.build_review_indicators(db, kyc)
    branches = (
        await db.execute(
            select(Branch).where(Branch.organization_id == organization_id)
        )
    ).scalars().all()
    return APIResponse(data={
        "kyc": {
            "organization_id": organization_id,
            "status": kyc.status,
            "tier": kyc.tier,
            "org_category": kyc.org_category,
            "legal_name": kyc.legal_name,
            "registration_number": kyc.registration_number,
            "representative_name": kyc.representative_name,
            "representative_verified": kyc.representative_verified,
            "review_decision": kyc.review_decision,
            "rejection_reason": kyc.rejection_reason,
            "more_info_request": kyc.more_info_request,
            "expires_at": kyc.expires_at,
            "appeal_status": kyc.appeal_status,
            "history": kyc.history,
        },
        "documents": docs,
        "branches": [
            {
                "id": b.id, "name": b.name, "city": b.city,
                "kyc_verified": b.kyc_verified,
            }
            for b in branches
        ],
        "indicators": indicators,
    })


@router.post("/admin/api/kyc/{organization_id}/review", response_model=APIResponse)
async def admin_kyc_review(
    organization_id: int,
    body: ts.KycReviewIn,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    try:
        kyc = await kyc_service.kyc_review(
            db, organization_id, admin.id, body.action,
            reason=body.reason, more_info_request=body.more_info_request,
            tier=body.tier, expires_at=body.expires_at,
            representative_verified=body.representative_verified,
        )
    except (ValueError, PermissionError) as e:
        status = 409 if isinstance(e, ValueError) else 403
        raise HTTPException(status, str(e))
    return APIResponse(message="Review recorded", data={"status": kyc.status})


@router.post("/admin/api/kyc/{organization_id}/appeal-decision", response_model=APIResponse)
async def admin_kyc_appeal_decision(
    organization_id: int,
    body: ts.KycAppealDecisionIn,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    try:
        kyc = await kyc_service.decide_appeal(
            db, organization_id, admin.id, body.decision, body.note
        )
    except (ValueError, PermissionError) as e:
        status = 409 if isinstance(e, ValueError) else 403
        raise HTTPException(status, str(e))
    return APIResponse(
        message="Appeal decided", data={"status": kyc.status, "appeal": kyc.appeal_status}
    )


@router.post("/admin/api/branches/{branch_id}/kyc-verify", response_model=APIResponse)
async def admin_branch_kyc(
    branch_id: int,
    verified: bool = True,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    try:
        branch = await kyc_service.verify_branch(db, branch_id, admin.id, verified)
    except LookupError as e:
        raise HTTPException(404, str(e))
    return APIResponse(
        message="Branch verification updated",
        data={"branch_id": branch.id, "kyc_verified": branch.kyc_verified},
    )


@router.get("/admin/api/kyc/{organization_id}/documents/{document_id}")
async def admin_kyc_document_download(
    organization_id: int, document_id: int,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    try:
        doc = await kyc_service.access_document(
            db, organization_id, document_id, "admin", admin.id
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    if not doc.document_url or not Path(doc.document_url).exists():
        raise HTTPException(404, "Document file not found")
    return FileResponse(doc.document_url, media_type="application/octet-stream")


@router.post("/admin/api/kyc/{organization_id}/purge-expired-documents", response_model=APIResponse)
async def admin_kyc_purge(
    organization_id: int,
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
):
    count = await kyc_service.purge_expired_documents(db, organization_id, admin.id)
    return APIResponse(message="Retention purge complete", data={"purged": count})
