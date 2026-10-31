"""
app/schemas/shift.py
───────────────────
Pydantic schemas for the daily counter shift state machine (SHIFT-001..006)
and assisted walk-in booking (PHY-001..004).
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import APIResponse


class CounterShiftOpenRequest(BaseModel):
    """Employee submission (SHIFT-001): counter, services, device
    readiness, opening cash and remarks."""

    counter_id: int = Field(..., ge=1)
    services: list[int] = Field(default_factory=list)
    device_ready: bool = False
    opening_cash: Decimal = Field(default=Decimal("0.00"), ge=0)
    remarks: Optional[str] = Field(None, max_length=2000)


class CounterShiftDecisionRequest(BaseModel):
    """Head decision (SHIFT-002): approve or reject with reason, approved
    services, optional approved daily period and payment permission."""

    decision: str = Field(..., pattern="^(approve|reject)$")
    reason: str = Field(..., min_length=1, max_length=2000)
    approved_services: Optional[list[int]] = None
    approved_period_start: Optional[time] = None
    approved_period_end: Optional[time] = None
    payment_permitted: Optional[bool] = None


class CounterShiftPauseRequest(BaseModel):
    """Operator request to pause shift (SHIFT-004)."""
    reason: Optional[str] = Field(None, max_length=500)


class CounterShiftHandoverRequest(BaseModel):
    """Operator request to hand over counter (SHIFT-004)."""
    target_employee_id: int = Field(..., ge=1)
    notes: Optional[str] = Field(None, max_length=1000)


class CounterShiftCloseRequest(BaseModel):
    """Employee shift closing submission (SHIFT-005): actual cash counted and remarks."""
    counted_cash: Decimal = Field(..., ge=0)
    remarks: Optional[str] = Field(None, max_length=2000)


class CounterShiftReconcileRequest(BaseModel):
    """Head reconciliation review and sign-off (SHIFT-006)."""
    action: str = Field(..., pattern="^(approve|request_correction|record_discrepancy|escalate)$")
    notes: Optional[str] = Field(None, max_length=2000)


class CounterShiftAdjustRequest(BaseModel):
    """Authorized adjustment to a reconciled shift (SHIFT-006)."""
    adjustment_amount: Decimal = Field(...)
    reason: str = Field(..., min_length=3, max_length=2000)


class CounterShiftClosingSummary(BaseModel):
    """Pre-closing summary of tokens, cash, and expected totals."""
    shift_id: int
    counter_id: int
    shift_date: date
    opening_cash: Decimal
    total_tokens_served: int
    total_cash_collected: Decimal
    total_online_collected: Decimal
    total_waived_amount: Decimal
    expected_cash: Decimal
    status_counts: dict[str, int]


class WalkinBookingRequest(BaseModel):
    """Assisted physical walk-in booking (PHY-001..004)."""
    counter_id: int = Field(..., ge=1)
    service_id: int = Field(..., ge=1)
    customer_name: str = Field(..., min_length=1, max_length=120)
    customer_phone: str = Field(..., min_length=10, max_length=20)
    customer_email: Optional[str] = None
    payment_method: str = Field(default="cash")
    waiver_reason: Optional[str] = None
    waiver_approved_by: Optional[str] = None
    waiver_evidence: Optional[str] = None
    client_reference: Optional[str] = None
    answers: Optional[dict[str, Any]] = None
    notes: Optional[str] = None


class CounterShiftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    counter_id: int
    employee_id: int
    shift_date: date
    status: str
    requested_services: list[int]
    device_ready: bool
    opening_cash: Decimal
    remarks: Optional[str]
    decided_at: Optional[datetime]
    decided_by_type: Optional[str]
    decided_by_id: Optional[int]
    decided_reason: Optional[str]
    approved_services: Optional[list[int]]
    approved_period_start: Optional[time]
    approved_period_end: Optional[time]
    payment_permitted: bool

    # SHIFT-004..006 fields
    paused_at: Optional[datetime] = None
    pause_reason: Optional[str] = None
    resumed_at: Optional[datetime] = None
    handover_to_employee_id: Optional[int] = None
    handover_notes: Optional[str] = None
    closed_at: Optional[datetime] = None
    closing_cash_counted: Optional[Decimal] = None
    closing_cash_expected: Optional[Decimal] = None
    closing_cash_variance: Optional[Decimal] = None
    total_tokens_served: int = 0
    total_cash_collected: Decimal = Decimal("0.00")
    total_online_collected: Decimal = Decimal("0.00")
    total_waived_amount: Decimal = Decimal("0.00")
    closing_remarks: Optional[str] = None
    reconciled_at: Optional[datetime] = None
    reconciled_by_type: Optional[str] = None
    reconciled_by_id: Optional[int] = None
    reconciliation_status: Optional[str] = None
    reconciliation_notes: Optional[str] = None
    adjustment_reason: Optional[str] = None
    adjustment_amount: Optional[Decimal] = None

    created_at: datetime
    updated_at: datetime


CounterShiftListResponse = APIResponse[list[CounterShiftRead]]
CounterShiftResponse = APIResponse[CounterShiftRead]
CounterShiftSummaryResponse = APIResponse[CounterShiftClosingSummary]
