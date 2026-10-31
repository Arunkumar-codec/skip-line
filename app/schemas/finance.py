"""
app/schemas/finance.py
──────────────────────
Pydantic schemas for Phase 5 Finance & Ledgering APIs.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── LED-001..004: Ledger Journal & Totals ────────────────────────────────────

class LedgerEntryRead(BaseModel):
    id: int
    organization_id: int
    account: str
    entry_type: str
    direction: str
    amount: Decimal
    currency: str
    token_id: Optional[int] = None
    payment_id: Optional[int] = None
    counter_shift_id: Optional[int] = None
    actor_type: Optional[str] = None
    actor_id: Optional[int] = None
    source_transaction_ref: Optional[str] = None
    description: Optional[str] = None
    entry_metadata: Optional[dict] = None
    created_at: datetime

    class Config:
        from_attributes = True


class LedgerTotalsRead(BaseModel):
    gross_revenue: Decimal
    cash_collected: Decimal
    gateway_collected: Decimal
    refunds: Decimal
    tax_collected: Decimal
    platform_fee: Decimal
    payouts_disbursed: Decimal
    net_revenue: Decimal


# ── PAY-003..005: Refunds ─────────────────────────────────────────────────────

class RefundCreateRequest(BaseModel):
    amount: Decimal = Field(gt=0, description="Amount to refund")
    reason: str = Field(min_length=3, max_length=500, description="Reason for refund")
    idempotency_key: Optional[str] = Field(None, max_length=100, description="Client idempotency key")


class RefundRead(BaseModel):
    id: int
    organization_id: int
    payment_id: int
    token_id: Optional[int] = None
    amount: Decimal
    currency: str
    reason: str
    status: str
    gateway_refund_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    requested_by_type: str
    requested_by_id: int
    approved_by_type: Optional[str] = None
    approved_by_id: Optional[int] = None
    rejection_reason: Optional[str] = None
    processed_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ── CASH-001..003: Cash Sessions & Breakdown ──────────────────────────────────

class CashSessionCloseRequest(BaseModel):
    counted_cash: Decimal = Field(ge=0, description="Total physical cash counted")
    denominations: Optional[dict[str, int]] = Field(None, description="Denomination count map, e.g. {'500': 2}")
    notes: Optional[str] = Field(None, max_length=500, description="Closing remarks or notes")


class CashSessionRead(BaseModel):
    id: int
    organization_id: int
    counter_shift_id: Optional[int] = None
    counter_id: int
    employee_id: int
    starting_float: Decimal
    collected_cash: Decimal
    counted_cash: Optional[Decimal] = None
    discrepancy: Optional[Decimal] = None
    status: str
    denominations: Optional[dict[str, Any]] = None
    notes: Optional[str] = None
    verified_by_id: Optional[int] = None
    created_at: datetime
    closed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── FIN-001..004: Payouts, Bank Accounts & Risk Flags ─────────────────────────

class PayoutHoldRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500, description="Reason for holding payout")


class PayoutRead(BaseModel):
    id: int
    organization_id: int
    amount: Decimal
    currency: str
    status: str
    hold_reason: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    gateway_payout_id: Optional[str] = None
    paid_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class OrgBankAccountUpdate(BaseModel):
    account_number: str = Field(min_length=8, max_length=30)
    ifsc_code: str = Field(min_length=5, max_length=20)
    account_holder_name: str = Field(min_length=2, max_length=150)
    bank_name: str = Field(min_length=2, max_length=100)


class OrgBankAccountRead(BaseModel):
    id: int
    organization_id: int
    account_number: str
    ifsc_code: str
    account_holder_name: str
    bank_name: str
    status: str
    held_until: Optional[datetime] = None
    hold_reason: Optional[str] = None
    verified_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class FinancialRiskFlagRead(BaseModel):
    id: int
    organization_id: int
    risk_type: str
    severity: str
    status: str
    description: str
    flag_metadata: Optional[dict[str, Any]] = None
    resolved_by: Optional[str] = None
    resolution_notes: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class RiskFlagResolveRequest(BaseModel):
    resolution_notes: str = Field(min_length=3, max_length=500)
    status: Optional[str] = Field("resolved", pattern="^(resolved|dismissed)$")


# ── FIN-001: payment-method policy & paid-booking pause ──────────────────────

class PaymentPolicyRead(BaseModel):
    organization_id: int
    allowed_payment_methods: Optional[list[str]] = None  # None = unrestricted


class PaymentPolicyUpdate(BaseModel):
    """Admin-only policy write. `allowed_payment_methods: null` (or omitted)
    lifts all restrictions; a list must contain only `PaymentMethod`
    vocabulary values and cannot be empty."""
    allowed_payment_methods: Optional[list[str]] = None


class PaidBookingHoldRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class PaidBookingHoldRead(BaseModel):
    """FIN-001 pause/resume state of a token (from_attributes over Token)."""
    token_id: int = Field(validation_alias="id")
    token_number: str
    organization_id: int
    status: str
    payment_hold: bool
    payment_hold_reason: Optional[str] = None
    payment_hold_at: Optional[datetime] = None
    payment_hold_by: Optional[int] = None
    payment_hold_previous_status: Optional[str] = None

    class Config:
        from_attributes = True


