"""
models/enforcement.py
─────────────────────
Phase 8 — Enforcement & Escalation Lifecycle (ENF-002..005).

The escalation ladder (ENF-002) is modelled as a CHAIN of
`EnforcementAction` rows per organization:

    warning → correction_deadline → scoped_restriction → suspension →
    permanent_block

Each escalation creates a NEW action row linking back to its predecessor
(`previous_action_id`) and flips the predecessor to SUPERSEDED — the full
history is preserved (evidence requirement, ENF-005). New status columns
are guarded plain Strings (no MySQL ENUM ALTERs), following the Standing
Phase-7 decision recorded in OPEN_DECISIONS.

Master requirement §4.4: high-impact actions support a second-person
review — suspending or permanently blocking an organization requires a
DISTINCT second approver (`enforcement_service` enforces identity
separation; `pre_suspension_impact` produces the ENF-003 evidence
snapshot).
"""

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class EnforcementType(str, enum.Enum):
    WARNING = "warning"
    CORRECTION_DEADLINE = "correction_deadline"
    SCOPED_RESTRICTION = "scoped_restriction"
    SUSPENSION = "suspension"
    PERMANENT_BLOCK = "permanent_block"


class EnforcementStatus(str, enum.Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"    # the organization corrected / complied
    REVOKED = "revoked"      # withdrawn by the platform
    SUPERSEDED = "superseded"  # escalated to a higher stage


class EnforcementScope(str, enum.Enum):
    ORGANIZATION = "organization"
    BRANCH = "branch"
    SERVICE = "service"
    COUNTER = "counter"


class EnforcementAction(Base, TimestampMixin):
    __tablename__ = "enforcement_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # Stage on the ESCALATION ladder (guarded String values — see module docstring)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False,
        default=EnforcementStatus.ACTIVE.value, index=True,
    )
    scope_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EnforcementScope.ORGANIZATION.value,
    )
    # FK target id for the narrowed scopes (branch_id / service_id / counter_id)
    scope_target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # ENF-005: immutable evidence bundle (risk flag ids, impact summary, file refs)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # CORRECTION_DEADLINE support (ENF-002: "correction deadline" stage)
    correction_deadline: Mapped["datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # SCOPED_RESTRICTION / SUSPENSION end (auto-expiry handled by the service)
    restricted_until: Mapped["datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # ENF-005: decision maker + evidence trail
    issued_by_type: Mapped[str] = mapped_column(String(20), nullable=False, default="admin")
    issued_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issued_by_label: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # §4.4 second-person approval (mandatory for suspension / permanent_block)
    second_approver_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    second_approver_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    second_approved_at: Mapped["datetime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    # Escalation chain: the action this one superseded
    previous_action_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("enforcement_actions.id", ondelete="SET NULL"), nullable=True,
    )

    resolved_by_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped["datetime | None"] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    organization: Mapped["Organization"] = relationship("Organization")
    appeals: Mapped[list["EnforcementAppeal"]] = relationship(
        "EnforcementAppeal", back_populates="action", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<EnforcementAction id={self.id} org={self.organization_id} "
            f"{self.action_type}/{self.status}>"
        )


class EnforcementAppeal(Base, TimestampMixin):
    """ENF-004: organization appeal against an enforcement action with an
    audited super-admin decision and optional re-verification request."""
    __tablename__ = "enforcement_appeals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    enforcement_action_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("enforcement_actions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="submitted", index=True,
    )
    reviewer_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    re_verification_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    decided_at: Mapped["datetime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    action: Mapped["EnforcementAction"] = relationship(
        "EnforcementAction", back_populates="appeals",
    )

    def __repr__(self) -> str:
        return f"<EnforcementAppeal id={self.id} action={self.enforcement_action_id} {self.status}>"
