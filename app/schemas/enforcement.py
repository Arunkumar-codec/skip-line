"""
app/schemas/enforcement.py
──────────────────────────
Phase 8 — enforcement API payloads (ENF-002..005).

Second-approver credentials arrive in the request body (never logged)
and are verified against the Super Admin store; the issuing admin and the
second approver must be DIFFERENT accounts (Master §4.4 dual approval).
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class EnforcementIssueIn(BaseModel):
    organization_id: int
    action_type: str = Field(
        description="warning | correction_deadline | scoped_restriction | suspension | permanent_block"
    )
    reason: str
    scope_type: str = "organization"
    scope_target_id: Optional[int] = None
    evidence: Optional[dict[str, Any]] = None
    correction_deadline: Optional[datetime] = None
    restricted_until: Optional[datetime] = None
    # §4.4 second-person approval (mandatory for suspension/permanent_block)
    second_approver_email: str = ""
    second_approver_password: str = ""


class EnforcementEscalateIn(BaseModel):
    new_action_type: str
    reason: str
    scope_type: Optional[str] = None
    scope_target_id: Optional[int] = None
    correction_deadline: Optional[datetime] = None
    restricted_until: Optional[datetime] = None
    second_approver_email: str = ""
    second_approver_password: str = ""


class EnforcementResolveIn(BaseModel):
    status: str = "resolved"  # resolved | revoked
    reason: str


class ReinstatementIn(BaseModel):
    reason: str
    second_approver_email: str = ""
    second_approver_password: str = ""


class SuspendOrgIn(BaseModel):
    reason: str
    evidence: Optional[dict[str, Any]] = None
    restricted_until: Optional[datetime] = None
    second_approver_email: str = ""
    second_approver_password: str = ""


class EnforcementAppealIn(BaseModel):
    reason: str


class EnforcementAppealDecisionIn(BaseModel):
    status: str  # approved | rejected
    decision_reason: str
    re_verification_requested: bool = False


class EnforcementAppealRead(BaseModel):
    id: int
    enforcement_action_id: int
    organization_id: int
    reason: str
    status: str
    reviewer_label: Optional[str] = None
    decision_reason: Optional[str] = None
    re_verification_requested: bool
    decided_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class EnforcementActionRead(BaseModel):
    id: int
    organization_id: int
    action_type: str
    status: str
    scope_type: str
    scope_target_id: Optional[int] = None
    reason: str
    evidence: Optional[dict[str, Any]] = None
    correction_deadline: Optional[datetime] = None
    restricted_until: Optional[datetime] = None
    issued_by_label: Optional[str] = None
    second_approver_label: Optional[str] = None
    second_approved_at: Optional[datetime] = None
    previous_action_id: Optional[int] = None
    resolved_by_label: Optional[str] = None
    resolved_reason: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
