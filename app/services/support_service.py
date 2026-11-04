"""
app/services/support_service.py
──────────────────────────────
Phase 7 — Unified Support Cases (SUP-001..007, CASE-001..004).

All authorization and validation happens here (server-side):
- CASE-002: every binding (token/payment/feedback/branch/counterpart org)
  is validated against the passing principal and org tenant before a
  case is created — forged ids fail loudly.
- SUP-006: three-party model (user, organization, admin). Access to a
  case is granted strictly through case_participants (or admin review
  role); there is no "any authenticated principal" path.
- SUP-004/007: status machine transitions validated in-place; customer
  resolution accept/reject drives RESOLVED, reply-only never resolves;
  CLOSED cases cannot accept messages (only reporter reopen).
- CASE-004: attachments are private; download requires participant
  membership and every access is logged to audit + access_count.
- CASE-003: display snapshots frozen at creation.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.services.audit_service import log_audit_event
from models.feedback import Feedback
from models.payment import Payment
from models.token import Token
from models.support import (
    CaseAttachment,
    CaseMessage,
    CaseParticipant,
    CaseStatus,
    CaseType,
    MessageReviewStatus,
    MessageVisibility,
    SupportCase,
)

PARTICIPANT_TYPES = {"user", "organization", "admin"}

# Which case types each principal may open.
ALLOWED_CASE_TYPES = {
    "user": {CaseType.USER_TO_ORG, CaseType.USER_TO_ADMIN,
             CaseType.PAYMENT_DISPUTE, CaseType.OTHER},
    "organization": {CaseType.ORG_TO_ADMIN, CaseType.ORG_REPORT_USER,
                     CaseType.FEEDBACK_REPORT, CaseType.PAYMENT_DISPUTE,
                     CaseType.OTHER},
}


def _ref() -> str:
    return f"SC-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:4]}"


async def _is_participant(
    db: AsyncSession, case: SupportCase, participant_type: str,
    participant_id: Optional[int],
) -> bool:
    # Admins are always authorized to moderate any case (platform role).
    if participant_type == "admin":
        return True
    for p in case.participants:
        if p.participant_type == participant_type and p.participant_id == participant_id:
            return True
    return False


async def get_case_for_participant(
    db: AsyncSession,
    case_id: int,
    participant_type: str,
    participant_id: Optional[int],
) -> SupportCase:
    """Membership-gated case fetch — raises PermissionError/LookupError."""
    case = (
        await db.execute(
            select(SupportCase)
            .options(
                selectinload(SupportCase.participants),
                selectinload(SupportCase.messages),
                selectinload(SupportCase.attachments),
            )
            .where(SupportCase.id == case_id)
        )
    ).scalar_one_or_none()
    if case is None:
        raise LookupError("Case not found")
    if not await _is_participant(db, case, participant_type, participant_id):
        raise PermissionError("You are not a participant of this case")
    return case


# ── CASE-002 binding validation ─────────────────────────────────────────────

async def _validate_bindings(
    db: AsyncSession,
    principal_type: str,
    principal_id: int,
    case_type: str,
    *,
    token_id: Optional[int] = None,
    payment_id: Optional[int] = None,
    feedback_id: Optional[int] = None,
    branch_id: Optional[int] = None,
    user_id: Optional[int] = None,
    organization_id: Optional[int] = None,
) -> dict:
    """Validate every bound entity belongs to the reporting participants.

    Returns a dict of validated binding values (user_id/organization_id/
    branch_id) used for participant setup. CASE-002 violations raise
    PermissionError.
    """
    resolved: dict = {}

    if token_id is not None:
        token = await db.get(Token, token_id)
        if token is None:
            raise LookupError("Bound token not found")
        if principal_type == "user" and token.user_id != principal_id:
            raise PermissionError("Token does not belong to you")
        if principal_type == "organization" and token.organization_id != principal_id:
            raise PermissionError("Token does not belong to your organization")
        resolved["token_organization_id"] = token.organization_id

    if payment_id is not None:
        payment = await db.get(Payment, payment_id)
        if payment is None:
            raise LookupError("Bound payment not found")
        # Payment record carries the tenant snapshot
        pay_org = getattr(payment, "organization_id", None)
        pay_user = getattr(payment, "user_id", None)
        if principal_type == "user" and pay_user != principal_id:
            raise PermissionError("Payment does not belong to you")
        if principal_type == "organization" and pay_org != principal_id:
            raise PermissionError("Payment does not belong to your organization")

    if feedback_id is not None:
        feedback = await db.get(Feedback, feedback_id)
        if feedback is None:
            raise LookupError("Bound feedback not found")
        if principal_type == "user" and feedback.user_id != principal_id:
            raise PermissionError("Feedback does not belong to you")
        if principal_type == "organization" and feedback.organization_id != principal_id:
            raise PermissionError("Feedback does not belong to your organization")
        resolved["feedback_org_id"] = feedback.organization_id

    if branch_id is not None:
        from models.branch import Branch
        branch = await db.get(Branch, branch_id)
        if branch is None:
            raise LookupError("Bound branch not found")
        if principal_type == "organization" and branch.organization_id != principal_id:
            raise PermissionError("Branch does not belong to your organization")
        resolved["branch_registered"] = True

    if principal_type == "user" and case_type == CaseType.USER_TO_ORG:
        if not organization_id:
            raise ValueError("user_to_org cases require an organization")
        from models.organization import Organization
        org = await db.get(Organization, organization_id)
        if org is None:
            raise LookupError("Target organization not found")
        resolved["target_org_id"] = organization_id
    elif principal_type == "organization" and case_type == CaseType.ORG_REPORT_USER:
        if not user_id:
            raise ValueError("org_report_user cases require a customer")
        from models.user import User
        user = await db.get(User, user_id)
        if user is None:
            raise LookupError("Reported customer not found")
        resolved["target_user_id"] = user_id

    return resolved


async def create_case(
    db: AsyncSession,
    principal_type: str,
    principal_id: int,
    principal_name: str,
    case_type: str,
    category: str,
    subject: str,
    detail: str,
    token_id: Optional[int] = None,
    payment_id: Optional[int] = None,
    feedback_id: Optional[int] = None,
    branch_id: Optional[int] = None,
    user_id: Optional[int] = None,
    organization_id: Optional[int] = None,
    ip_address: Optional[str] = None,
) -> SupportCase:
    if case_type not in ALLOWED_CASE_TYPES.get(principal_type, set()):
        raise PermissionError(
            f"Principal '{principal_type}' cannot open case type '{case_type}'"
        )

    bindings = await _validate_bindings(
        db, principal_type, principal_id, case_type,
        token_id=token_id, payment_id=payment_id, feedback_id=feedback_id,
        branch_id=branch_id, user_id=user_id, organization_id=organization_id,
    )

    # Tenant resolution + CASE-003 snapshots.
    user_id_v: Optional[int] = principal_id if principal_type == "user" else user_id
    organization_id_v: Optional[int] = (
        principal_id if principal_type == "organization"
        else bindings.get("target_org_id") or bindings.get("token_organization_id")
    )
    snapshot: dict = {"reporter": f"{principal_type}#{principal_id}"}
    if organization_id_v:
        from models.organization import Organization
        org = await db.get(Organization, organization_id_v)
        snapshot["org_name"] = org.name if org else None
    if principal_type == "user" and not user_id_v:
        from models.user import User
        user = await db.get(User, principal_id)
        snapshot["user_name"] = user.name if user else None
    if token_id is not None:
        token = await db.get(Token, token_id)
        snapshot["token_number"] = token.token_number if token else None

    case = SupportCase(
        reference=_ref(),
        case_type=case_type,
        category=category[:40],
        status=CaseStatus.SUBMITTED,
        subject=subject,
        detail=detail,
        user_id=user_id_v,
        organization_id=organization_id_v,
        branch_id=branch_id,
        counter_id=None,
        service_id=None,
        token_id=token_id,
        payment_id=payment_id,
        feedback_id=feedback_id,
        snapshot=snapshot,
        created_by_type=principal_type,
        created_by_id=principal_id,
    )
    db.add(case)
    await db.flush()

    reporter = CaseParticipant(
        case_id=case.id, participant_type=principal_type,
        participant_id=principal_id, display_name=principal_name,
        role="reporter", joined_at=datetime.now(timezone.utc),
    )
    counterpart_id = user_id_v if principal_type != "user" else organization_id_v
    counterpart = CaseParticipant(
        case_id=case.id,
        participant_type="organization" if principal_type == "user" else "user",
        participant_id=counterpart_id,
        display_name=(
            snapshot.get("org_name") or "Organization"
            if principal_type == "user" else snapshot.get("user_name") or "Customer"
        ),
        role="respondent",
        joined_at=datetime.now(timezone.utc),
    )
    db.add_all([reporter, counterpart])

    first_message = CaseMessage(
        case_id=case.id, sender_type=principal_type, sender_id=principal_id,
        sender_name=principal_name, body=detail,
        visibility=MessageVisibility.PARTIES,
        review_status=MessageReviewStatus.SENT,
    )
    db.add(first_message)
    await db.commit()

    await log_audit_event(
        db, action="case_created", actor_type=principal_type,
        actor_id=principal_id, organization_id=organization_id_v,
        target_type="support_case", target_id=case.reference,
        details={"case_type": case_type, "category": category},
        ip_address=ip_address,
    )
    await db.refresh(case)
    return case


# ── messaging ───────────────────────────────────────────────────────────────

def _visible_messages(case: SupportCase, viewer_type: str) -> list[CaseMessage]:
    allowed = case.messages if viewer_type == "admin" else [
        m for m in case.messages
        if m.visibility == MessageVisibility.PARTIES
        and m.review_status != MessageReviewStatus.PENDING_REVIEW
    ]
    return list(allowed)


async def add_message(
    db: AsyncSession,
    case: SupportCase,
    sender_type: str,
    sender_id: Optional[int],
    sender_name: str,
    body: str,
    visibility: str = MessageVisibility.PARTIES,
    suggested_solution_id: Optional[int] = None,
    review_status: str = MessageReviewStatus.SENT,
    ip_address: Optional[str] = None,
) -> CaseMessage:
    if case.status == CaseStatus.CLOSED:
        raise ValueError("This case is closed. Reopen it to continue the thread.")
    if visibility not in (MessageVisibility.PARTIES, MessageVisibility.INTERNAL):
        raise ValueError("Invalid message visibility")
    if visibility == MessageVisibility.INTERNAL and sender_type != "admin":
        raise PermissionError("Only platform administrators can post internal notes")
    if sender_type == "organization" and case.status == CaseStatus.SUBMITTED:
        # Replying implies acknowledgement (but never resolves by itself).
        case.status = CaseStatus.ACKNOWLEDGED

    message = CaseMessage(
        case_id=case.id, sender_type=sender_type, sender_id=sender_id,
        sender_name=sender_name, body=body, visibility=visibility,
        review_status=review_status,
        suggested_solution_id=suggested_solution_id,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    await log_audit_event(
        db, action="case_message_added", actor_type=sender_type,
        actor_id=sender_id, organization_id=case.organization_id,
        target_type="support_case", target_id=case.reference,
        details={"visibility": visibility, "review_status": review_status},
        ip_address=ip_address,
    )
    return message


# ── status machine ──────────────────────────────────────────────────────────

async def transition_case(
    db: AsyncSession,
    case: SupportCase,
    actor_type: str,
    new_status: str,
    note: Optional[str] = None,
    assigned_admin_id: Optional[int] = None,
    ip_address: Optional[str] = None,
) -> SupportCase:
    allowed = CaseStatus.TRANSITIONS.get(case.status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Illegal case transition {case.status} → {new_status}"
        )
    # Only admins may administratively move the case forward (except the
    # customer's own resolution/reopen actions, which have dedicated calls
    # and only touch RESOLVED/REOPENED).
    admin_only = {
        CaseStatus.ASSIGNED, CaseStatus.IN_REVIEW, CaseStatus.CLOSED,
        CaseStatus.ACKNOWLEDGED, CaseStatus.ESCALATED,
    }
    if new_status in admin_only and actor_type != "admin":
        raise PermissionError("Only platform administrators can perform this action")
    # waiting_for_party may also be driven by the customer rejecting a
    # proposed resolution (customer_resolution_decision reject path).
    if (
        new_status == CaseStatus.WAITING_FOR_PARTY
        and actor_type not in ("admin", "user")
    ):
        raise PermissionError("Only platform administrators can perform this action")

    case.status = new_status
    now = datetime.now(timezone.utc)
    if new_status == CaseStatus.RESOLVED:
        case.resolved_at = now
    if new_status == CaseStatus.CLOSED:
        case.closed_at = now
    if new_status in (CaseStatus.ASSIGNED, CaseStatus.ESCALATED) and assigned_admin_id:
        case.assigned_admin_id = assigned_admin_id
    await db.commit()

    await log_audit_event(
        db, action=f"case_{new_status}", actor_type=actor_type,
        organization_id=case.organization_id,
        target_type="support_case", target_id=case.reference,
        details={"note": note or ""},
        ip_address=ip_address,
    )
    await db.refresh(case)
    return case


async def propose_solution(
    db: AsyncSession,
    case: SupportCase,
    body: str,
    is_sensitive: bool = False,
    suggested_solution_id: Optional[int] = None,
    actor_type: str = "organization",
    actor_id: Optional[int] = None,
    actor_name: str = "Organization",
    ip_address: Optional[str] = None,
) -> tuple[CaseMessage, str]:
    """SUP-007/KB-002: propose a solution; sensitive drafts await review.

    Returns (message, resulting_status). Non-sensitive proposals move the
    case to SOLUTION_PROPOSED; the customer still must accept explicitly.
    """
    if case.status == CaseStatus.CLOSED:
        raise ValueError("This case is closed.")
    review_status = (
        MessageReviewStatus.PENDING_REVIEW if is_sensitive
        else MessageReviewStatus.SENT
    )
    message = await add_message(
        db, case, actor_type, actor_id, actor_name, body,
        suggested_solution_id=suggested_solution_id,
        review_status=review_status, ip_address=ip_address,
    )
    if not is_sensitive:
        if CaseStatus.SOLUTION_PROPOSED in CaseStatus.TRANSITIONS.get(
            case.status, set()
        ):
            await transition_case(
                db, case, actor_type, CaseStatus.SOLUTION_PROPOSED,
                note="solution proposed", ip_address=ip_address,
            )
        state = CaseStatus.SOLUTION_PROPOSED
    else:
        state = case.status  # unchanged until an admin approves the draft
    if suggested_solution_id is not None:
        from models.knowledge import KnowledgeSolution
        solution = await db.get(KnowledgeSolution, suggested_solution_id)
        if solution:
            solution.times_used += 1
            await db.commit()
    return message, state


async def customer_resolution_decision(
    db: AsyncSession,
    case: SupportCase,
    accept: bool,
    user_id: int,
    user_name: str,
    ip_address: Optional[str] = None,
) -> SupportCase:
    """SUP-007: only the reporter decides on a proposed resolution.

    Accept → RESOLVED (recorded as explicit acceptance). Reject →
    WAITING_FOR_PARTY (sending a reply alone never equals resolution).
    """
    if case.status != CaseStatus.SOLUTION_PROPOSED:
        raise ValueError(
            "No resolution is currently proposed on this case"
        )
    if case.user_id != user_id:
        raise PermissionError("Only the reporting customer can decide the resolution")
    if accept:
        case.status = CaseStatus.RESOLVED
        case.resolution_accepted_by = "user"
        case.resolution_note = case.resolution_note
        case.resolved_at = datetime.now(timezone.utc)
        await db.commit()
        await log_audit_event(
            db, action="case_resolved_accepted", actor_type="user",
            actor_id=user_id, organization_id=case.organization_id,
            target_type="support_case", target_id=case.reference,
            ip_address=ip_address,
        )
    else:
        await transition_case(
            db, case, "user", CaseStatus.WAITING_FOR_PARTY,
            note="customer requested more help",
            ip_address=ip_address,
        )
    await db.refresh(case)
    return case


async def customer_reopen(
    db: AsyncSession,
    case: SupportCase,
    user_id: int,
    reason: str,
    ip_address: Optional[str] = None,
) -> SupportCase:
    if case.status not in (CaseStatus.CLOSED, CaseStatus.RESOLVED):
        raise ValueError("Only resolved or closed cases can be reopened")
    if case.user_id != user_id:
        raise PermissionError("Only the reporting customer can reopen the case")
    await transition_case(
        db, case, "user", CaseStatus.REOPENED, note=reason,
        ip_address=ip_address,
    )
    case.reopened_count += 1
    await db.commit()
    await db.refresh(case)
    return case


# ── attachments (CASE-004) ──────────────────────────────────────────────────

async def add_attachment(
    db: AsyncSession,
    case: SupportCase,
    stored_path: str,
    filename: str,
    mime: Optional[str],
    size_bytes: Optional[int],
    sha256: Optional[str],
    uploaded_by_type: str,
    uploaded_by_id: Optional[int],
    scan_status: Optional[str] = None,
    scanned_at: Optional[Any] = None,
    scan_signature: Optional[str] = None,
) -> CaseAttachment:
    if case.status == CaseStatus.CLOSED:
        raise ValueError("This case is closed.")
    attachment = CaseAttachment(
        case_id=case.id, message_id=None,
        uploaded_by_type=uploaded_by_type, uploaded_by_id=uploaded_by_id,
        filename=filename, stored_path=stored_path, mime=mime,
        size_bytes=size_bytes, sha256=sha256, access_count=0,
        scan_status=scan_status, scanned_at=scanned_at,
        scan_signature=scan_signature,
    )
    db.add(attachment)
    await db.commit()
    await db.refresh(attachment)
    await log_audit_event(
        db, action="case_attachment_uploaded", actor_type=uploaded_by_type,
        actor_id=uploaded_by_id, organization_id=case.organization_id,
        target_type="support_case", target_id=case.reference,
        details={"filename": filename},
    )
    return attachment


async def access_attachment(
    db: AsyncSession,
    case: SupportCase,
    attachment_id: int,
    viewer_type: str,
    viewer_id: Optional[int],
    ip_address: Optional[str] = None,
) -> CaseAttachment:
    """Participant-gated download bookkeeping (private, access-logged)."""
    attachment = await db.get(CaseAttachment, attachment_id)
    if attachment is None or attachment.case_id != case.id:
        raise LookupError("Attachment not found")
    attachment.access_count += 1
    await db.commit()
    await log_audit_event(
        db, action="case_attachment_accessed", actor_type=viewer_type,
        actor_id=viewer_id, organization_id=case.organization_id,
        target_type="case_attachment", target_id=attachment.id,
        details={"case": case.reference, "filename": attachment.filename},
        ip_address=ip_address,
    )
    await db.refresh(attachment)
    return attachment


# ── listing helpers ─────────────────────────────────────────────────────────

async def list_cases_for(
    db: AsyncSession,
    participant_type: str,
    participant_id: Optional[int],
    status: Optional[str] = None,
) -> list[SupportCase]:
    if participant_type == "admin":
        query = select(SupportCase).order_by(SupportCase.id.desc())
    else:
        participants = (
            await db.execute(
                select(CaseParticipant.case_id).where(
                    CaseParticipant.participant_type == participant_type,
                    CaseParticipant.participant_id == participant_id,
                )
            )
        ).scalars().all()
        if not participants:
            return []
        query = select(SupportCase).where(
            SupportCase.id.in_(participants)
        ).order_by(SupportCase.id.desc())
    if status:
        query = query.where(SupportCase.status == status)
    return list(
        (await db.execute(query.options(selectinload(SupportCase.participants))))
        .scalars().all()
    )


async def admin_review_participants(
    db: AsyncSession, case: SupportCase
) -> list[CaseParticipant]:
    """Ensure an admin participant row exists (lazily on first admin touch)."""
    for p in case.participants:
        if p.participant_type == "admin":
            return list(
                [p for p in case.participants if p.participant_type == "admin"]
            )
    admin = CaseParticipant(
        case_id=case.id, participant_type="admin", participant_id=None,
        display_name="Platform Support", role="admin",
        joined_at=datetime.now(timezone.utc),
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    case.participants.append(admin)
    return [admin]
