"""
models/workflow.py
──────────────────
Configurable multi-stage service workflows (MASTER §10, Phase 9 Workstream B).

An organization MAY attach a workflow to one of its services, describing an
 ordered list of stages through which a customer progresses (illustrative
 healthcare example: Registration → Measurement → Counselling → Pharmacy →
 Prescription advice). Stages are fully organization-defined data — nothing
 is hardcoded for any industry.

One workflow per service ( UniqueConstraint); services without a workflow
 keep the legacy single-counter flow untouched (B15).

Stage progression is tracked per token with a unique state row advanced via
 optimistic compare-and-swap (UPDATE ... WHERE current_stage = expected), so
 concurrent employee actions at the same stage serialise safely instead of
 double-advancing. Every transition writes an append-only stage log with the
 employee identity, reason (required for recalls/cancels) and string
 snapshots of the stage definition, so later stage reconfiguration never
 rewrites history.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, func, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class ServiceWorkflow(Base, TimestampMixin):
    """An organization-configured ordered workflow attached to one service."""

    __tablename__ = "service_workflows"
    __table_args__ = (
        UniqueConstraint("service_id", name="uq_service_workflow"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    service_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("services.id", ondelete="CASCADE"),
        nullable=False, unique=True,  # B2/B15: one workflow per service; unconfigured = legacy flow
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    stages: Mapped[list["WorkflowStage"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowStage.position",
        lazy="selectin",
    )


class WorkflowStage(Base, TimestampMixin):
    """A single stage of a service workflow (B3: stable code, name, order)."""

    __tablename__ = "workflow_stages"
    __table_args__ = (
        UniqueConstraint("workflow_id", "code", name="uq_workflow_stage_code"),
        UniqueConstraint("workflow_id", "position", name="uq_workflow_stage_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("service_workflows.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Stable machine identifier (B3) — e.g. "registration", "pharmacy".
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based order
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # B4: mandatory stages error on skip; non-mandatory (skippable) stages may be skipped.
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # B12 contribution: minutes this stage is expected to take.
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Optional counter binding (B2/B6): when set, stage processing is
    # authorized to employees with an active shift at THIS counter.
    counter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    workflow: Mapped[ServiceWorkflow] = relationship(back_populates="stages")


class TokenWorkflowState(Base, TimestampMixin):
    """Per-token stage progression (unique token; CAS-advanced)."""

    __tablename__ = "token_workflow_states"
    __table_args__ = (
        UniqueConstraint("token_id", name="uq_token_workflow_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"),
        nullable=False, index=True, unique=True,
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    workflow_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("service_workflows.id", ondelete="RESTRICT"), nullable=False
    )
    # The stage the customer is CURRENTLY AT for processing; NULL once the
    # workflow reaches a terminal state (completed/cancelled).
    current_stage_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("workflow_stages.id", ondelete="SET NULL"), nullable=True
    )
    # active → completed | cancelled
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    # Optimistic concurrency ticket (B13): bumped by every CAS transition.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TokenStageLog(Base, TimestampMixin):
    """Append-only stage transition history (B9): snapshots, identity, reason."""

    __tablename__ = "token_stage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    state_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("token_workflow_states.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # start | advance | skip | recall | complete | cancel
    action: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    from_stage_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_stage_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Snapshot of the stage definition AT TRANSITION TIME (B9: history is
    # immutable even when the org later renames/reorders/reconfigures stages).
    stage_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    stage_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    stage_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage_mandatory: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    employee_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
