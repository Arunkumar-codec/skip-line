"""
models/counter_shift.py
──────────────────────
Daily counter operational shift (SHIFT-001/002).

Workflow: the counter operator submits an open request (counter, services,
device readiness, opening cash, remarks) and the shift becomes ACTIVE only
after an authorized head (organization principal or an employee delegated
via the `shifts:approve` permission) approves it. Status progression:

  OPEN_REQUESTED -> APPROVED -> ACTIVE   (this task, SHIFT-001/002)
  CLOSED                                   (terminal; closing/reconciliation
                                            transitions are SHIFT-004/005/006)
  PAUSED, CLOSE_REQUESTED, RECONCILING     reserved for the closing workflow

A rejected request stays OPEN_REQUESTED with the head's recorded reason so
the employee can correct and resubmit (no `REJECTED` status exists in the
requirements; see OPEN_DECISIONS Shift notes).
"""

import enum
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class CounterShiftStatus(str, enum.Enum):
    """Lifecycle of a daily counter shift (roadmap Phase 4 enum)."""

    CLOSED = "closed"
    OPEN_REQUESTED = "open_requested"
    APPROVED = "approved"
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSE_REQUESTED = "close_requested"
    RECONCILING = "reconciling"


# Statuses an operator request may not collide with: a tenant can have only
# one non-terminal shift per (employee, counter, day) — CLOSED is terminal.
ACTIVE_SHIFT_STATUSES = (
    CounterShiftStatus.OPEN_REQUESTED,
    CounterShiftStatus.APPROVED,
    CounterShiftStatus.ACTIVE,
    CounterShiftStatus.PAUSED,
    CounterShiftStatus.CLOSE_REQUESTED,
    CounterShiftStatus.RECONCILING,
)


class CounterShift(Base, TimestampMixin):
    __tablename__ = "counter_shifts"
    __table_args__ = (
        Index("ix_shifts_org_date_status", "organization_id", "shift_date", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    counter_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    shift_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(CounterShiftStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=CounterShiftStatus.OPEN_REQUESTED,
    )

    # SHIFT-001: submitted request content
    requested_services: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    device_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opening_cash: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    # SHIFT-002: head decision
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decided_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_services: Mapped[list | None] = mapped_column(JSON, nullable=True)
    approved_period_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    approved_period_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    payment_permitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # SHIFT-004: pause, resume and handover
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handover_to_employee_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    handover_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # SHIFT-005: closing report and financial reconciliation
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closing_cash_counted: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    closing_cash_expected: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    closing_cash_variance: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    total_tokens_served: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cash_collected: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    total_online_collected: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    total_waived_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    closing_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    # SHIFT-006: head reconciliation review & sign-off
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciled_by_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reconciled_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reconciliation_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reconciliation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    adjustment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    adjustment_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization")
    counter: Mapped["Counter"] = relationship("Counter")
    employee: Mapped["Employee"] = relationship("Employee", foreign_keys=[employee_id])
    handover_to_employee: Mapped["Employee | None"] = relationship(
        "Employee", foreign_keys=[handover_to_employee_id]
    )

    def __repr__(self) -> str:
        return (
            f"<CounterShift id={self.id} counter_id={self.counter_id} "
            f"employee_id={self.employee_id} date={self.shift_date} "
            f"status={self.status}>"
        )
