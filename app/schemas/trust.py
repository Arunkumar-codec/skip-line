"""
app/schemas/trust.py
───────────────────
Phase 7 Pydantic schemas: verified feedback, unified support cases,
knowledge solutions and organization KYC.

Read schemas exclude raw contact numbers, KYC documents, storage paths
and internal audit data — list/public representations are display-safe.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Feedback (FDB) ───────────────────────────────────────────────────────────

class FeedbackSubmit(BaseModel):
    rating_overall: int = Field(ge=1, le=5)
    rating_waiting: Optional[int] = Field(None, ge=1, le=5)
    rating_staff: Optional[int] = Field(None, ge=1, le=5)
    rating_service: Optional[int] = Field(None, ge=1, le=5)
    result: Optional[str] = Field(None, pattern="^(resolved|partial|unresolved)$")
    comment: Optional[str] = Field(None, max_length=4000)
    is_anonymous: bool = False


class FeedbackResponseIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class FeedbackRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rating_overall: int
    rating_waiting: Optional[int]
    rating_staff: Optional[int]
    rating_service: Optional[int]
    result: Optional[str]
    comment: Optional[str]
    is_anonymous: bool
    moderation_status: str
    has_attachment: bool = False
    # Display-safe context (never token ids, contacts or file paths).
    service_name: Optional[str] = None
    counter_name: Optional[str] = None
    period_label: Optional[str] = None       # e.g. "Nov 2026"
    display_author: Optional[str] = None     # name or "Verified customer"
    org_response: Optional[str] = None
    org_reported: bool = False
    created_at: Optional[datetime] = None


class FeedbackModerateIn(BaseModel):
    action: str = Field(pattern="^(publish|hide|remove|report)$")
    reason: Optional[str] = Field(None, max_length=500)


# ── Support cases (SUP / CASE) ──────────────────────────────────────────────

class CaseCreate(BaseModel):
    case_type: str
    category: str = Field(max_length=40)
    subject: str = Field(min_length=3, max_length=200)
    detail: str = Field(min_length=1, max_length=10000)
    # Bindings are validated server-side (CASE-002); users may only bind
    # entities they verifiably own, organizations only their own.
    token_id: Optional[int] = None
    payment_id: Optional[int] = None
    feedback_id: Optional[int] = None
    branch_id: Optional[int] = None
    user_id: Optional[int] = None          # org-reported customer (org cases)
    organization_id: Optional[int] = None  # user-to-org target (user cases)


class CaseMessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


class CaseSuggestIn(BaseModel):
    """KB-002: propose a knowledge solution into a case conversation."""
    knowledge_solution_id: int
    body_override: Optional[str] = Field(None, max_length=8000)


class CaseTransitionIn(BaseModel):
    action: str
    note: Optional[str] = Field(None, max_length=2000)


class CaseParticipantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    participant_type: str
    display_name: str
    role: str


class AttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    size_bytes: Optional[int]
    mime: Optional[str]
    uploaded_by_type: Optional[str]


class CaseMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sender_type: str
    sender_name: str
    body: str
    visibility: str
    review_status: str


class CaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    case_type: str
    category: str
    status: str
    subject: str
    detail: str
    snapshot: Optional[dict]
    reopened_count: int
    resolution_note: Optional[str]
    created_at: Optional[datetime] = None


class CaseDetailResponse(BaseModel):
    case: CaseRead
    participants: list[CaseParticipantRead]
    messages: list[CaseMessageRead]
    attachments: list[AttachmentRead]


# ── Knowledge solutions (KB) ────────────────────────────────────────────────

class KnowledgeIn(BaseModel):
    category: str = Field(pattern="^(faq|troubleshooting|refund|service_response|other)$")
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    audience: str = Field(default="user", pattern="^(user|organization|agent)$")
    sensitive: bool = False


class KnowledgeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: Optional[int]
    category: str
    title: str
    body: str
    audience: str
    sensitive: bool
    is_active: bool
    times_used: int
    created_by_type: Optional[str]


# ── Organization KYC (ORGKYC) ───────────────────────────────────────────────

class KycApplicationIn(BaseModel):
    legal_name: str = Field(min_length=2, max_length=255)
    registration_number: str = Field(min_length=2, max_length=100)
    org_category: str = Field(min_length=2, max_length=40)
    registered_address: str = Field(min_length=5, max_length=500)
    operating_address: Optional[str] = Field(None, max_length=500)
    contact_email: str = Field(min_length=5, max_length=255)
    contact_phone: str = Field(min_length=5, max_length=30)
    representative_name: str = Field(min_length=2, max_length=150)
    representative_role: str = Field(min_length=2, max_length=100)
    representative_email: str = Field(min_length=5, max_length=255)
    representative_phone: str = Field(min_length=5, max_length=30)


class KycDocIn(BaseModel):
    """JSON path for documents already archived as file-less metadata."""
    document_type: str = Field(min_length=2, max_length=100)
    document_number: str = Field(min_length=1, max_length=100)
    expires_at: Optional[datetime] = None


class KycSubmitIn(BaseModel):
    note: Optional[str] = Field(None, max_length=2000)


class KycReviewIn(BaseModel):
    action: str = Field(
        pattern="^(under_review|more_info|approve|reject|restrict|suspend|block|unblock)$"
    )
    reason: Optional[str] = Field(None, max_length=500)
    more_info_request: Optional[str] = Field(None, max_length=4000)
    tier: Optional[str] = Field(None, pattern="^(base|standard|premium)$")
    expires_at: Optional[datetime] = None
    representative_verified: Optional[bool] = None


class KycAppealIn(BaseModel):
    reason: str = Field(min_length=10, max_length=4000)


class KycAppealDecisionIn(BaseModel):
    decision: str = Field(pattern="^(approved|denied)$")
    note: Optional[str] = Field(None, max_length=500)


class KycRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    status: str
    tier: str
    legal_name: Optional[str]
    org_category: Optional[str]
    registered_address: Optional[str]
    contact_email: Optional[str]
    contact_phone: Optional[str]
    # Representative contacts are masked for non-approvers; raw phone/email
    # of the representative are never serialized from this schema.
    representative_name: Optional[str] = None
    representative_role: Optional[str]
    representative_verified: bool
    rejection_reason: Optional[str]
    more_info_request: Optional[str]
    expires_at: Optional[datetime]
    appeal_status: Optional[str]
    reviewed_at: Optional[datetime]
    history: Optional[list] = None


class KycDocRead(BaseModel):
    id: int
    document_type: str
    document_number_masked: str
    verification_status: str
    expires_at: Optional[datetime]
    expired: bool = False
