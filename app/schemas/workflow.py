"""
app/schemas/workflow.py
──────────────
Schemas for configurable multi-stage service workflows (Phase 9 §10).
"""

from typing import Any, Optional
from pydantic import BaseModel, Field


class WorkflowStageIn(BaseModel):
    """Stage definition as submitted by an org administrator (B3/B4).

    `position` defaults to the stage's list index (validated as contiguous
    1..n by the service); it may be given explicitly for clarity.
    """
    code: str = Field(min_length=1, max_length=50,
                      description="Stable machine identifier, e.g. 'pharmacy'")
    name: str = Field(min_length=1, max_length=200)
    position: Optional[int] = Field(default=None, ge=1,
                                    description="Defaults to the list position")
    description: Optional[str] = None
    is_mandatory: bool = True
    # B4: mandatory stages refuse skip; False (skippable) allows skip.
    estimated_minutes: int = Field(default=0, ge=0, le=1440)
    # Optional counter binding: stage processing authorized on that counter's
    # active shift instead of the booking counter.
    counter_id: Optional[int] = None


class WorkflowCreate(BaseModel):
    """B1: create a workflow for a service (one per service)."""
    service_id: int
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    is_active: bool = True
    # The illustrative 5-stage healthcare example is DATA (see README/docs);
    # a workflow may also be a single stage for trivially staged services.
    stages: list[WorkflowStageIn] = Field(min_length=1, max_length=50)


class WorkflowUpdate(BaseModel):
    """B1: rename, (de)activate, or replace the stage list (replace-all)."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    is_active: Optional[bool] = None
    stages: Optional[list[WorkflowStageIn]] = None


class WorkflowStageRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    code: str
    name: str
    position: int
    description: Optional[str] = None
    is_mandatory: bool
    estimated_minutes: int
    counter_id: Optional[int] = None
    is_active: bool


class WorkflowRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    organization_id: int
    service_id: int
    name: str
    description: Optional[str] = None
    is_active: bool
    stages: list[WorkflowStageRead] = []


class StageTransitionRequest(BaseModel):
    """Employee stage transition input (B7/B8). Recall REQUIRES reason (B8)."""
    reason: Optional[str] = Field(default=None, max_length=2000)
    target_stage_code: Optional[str] = Field(
        default=None, max_length=50,
        description="Recall target (an EARLIER stage code) — required for recall",
    )


class StageHistoryEntry(BaseModel):
    action: str
    from_stage: Optional[str] = None
    to_stage: Optional[str] = None
    stage: Optional[str] = None
    employee: Optional[str] = None
    reason: Optional[str] = None
    at: Optional[str] = None


class StageProgressStage(BaseModel):
    code: str
    name: str
    position: int
    description: Optional[str] = None
    is_mandatory: bool = True
    estimated_minutes: int = 0
    # pending | in_progress | completed | skipped
    state: str = "pending"


class StageProgressRead(BaseModel):
    """B11 progress view — customers (via tracking) and employees (API)."""
    workflow_id: int
    workflow_name: str
    status: str
    version: int
    current_stage: Optional[dict[str, Any]] = None
    stages: list[StageProgressStage] = []
    stage_minutes_remaining: int = 0
    history: list[StageHistoryEntry] = []
