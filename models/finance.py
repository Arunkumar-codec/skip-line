"""
models/finance.py
─────────────────
Phase 5: Finance & Immutable Ledger Data Models.

Implements institutional-grade financial integrity:
  - LED-001..004: Append-only double-entry ledger entries linking to org, token,
    payment, shift, actor and source transaction.
  - CASH-001..003: Physical cash drawer sessions, denomination breakdown, and
    shift closing discrepancy reconciliation.
  - PAY-003..005: Idempotent refunds, webhooks, and gateway settlement records.
  - FIN-001..004: Organization bank account verification with 72h payout holds,
    payout management, and proportional risk evaluation flags.
"""

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


# ── LED-001..004: Double-Entry Immutable Ledger ───────────────────────────────

class LedgerAccount(str, enum.Enum):
    CASH = "cash"
    GATEWAY = "gateway"
    RECEIVABLES = "receivables"
    REVENUE = "revenue"
    PLATFORM_FEE = "platform_fee"
    TAX_PAYABLE = "tax_payable"
    REFUNDS = "refunds"
    PAYOUTS = "payouts"
    ADJUSTMENTS = "adjustments"


class LedgerEntryType(str, enum.Enum):
    CHARGE = "charge"
    TAX = "tax"
    PLATFORM_FEE = "platform_fee"
    DISCOUNT = "discount"
    REFUND = "refund"
    CASH = "cash"
    ADJUSTMENT = "adjustment"
    CHARGEBACK = "chargeback"
    SETTLEMENT = "settlement"
    PAYOUT = "payout"


class LedgerDirection(str, enum.Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class LedgerEntry(Base):
    """
    Append-only double-entry financial ledger journal.
    Completed entries are immutable; corrections must post explicit reversals.
    """
    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_org_created", "organization_id", "created_at"),
        Index("ix_ledger_account_type", "account", "entry_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entry_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # 'debit' or 'credit'
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="INR")

    token_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    counter_shift_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("counter_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )

    actor_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_transaction_ref: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization")
    token: Mapped["Token | None"] = relationship("Token")
    payment: Mapped["Payment | None"] = relationship("Payment")
    counter_shift: Mapped["CounterShift | None"] = relationship("CounterShift")

    def __repr__(self) -> str:
        return (
            f"<LedgerEntry id={self.id} org={self.organization_id} {self.direction} "
            f"{self.account} {self.amount} {self.entry_type}>"
        )


# ── CASH-001..003: Physical Cash Drawer Session ──────────────────────────────

class CashSessionStatus(str, enum.Enum):
    OPEN = "open"
    MATCHED = "matched"
    SHORTAGE = "shortage"
    EXCESS = "excess"
    PENDING_VERIFICATION = "pending_verification"
    CLOSED = "closed"


class CashSession(Base, TimestampMixin):
    """
    Physical cash session per counter/shift drawer.
    Supports denomination breakdown and discrepancy reporting.
    """
    __tablename__ = "cash_sessions"
    __table_args__ = (
        Index("ix_cash_sessions_org_shift", "organization_id", "counter_shift_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    counter_shift_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("counter_shifts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    counter_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("counters.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    starting_float: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    collected_cash: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    counted_cash: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    discrepancy: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=CashSessionStatus.OPEN.value, index=True
    )

    # Physical cash denomination breakdown: e.g. {"500": 2, "200": 5, "100": 10}
    denominations: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization")
    counter_shift: Mapped["CounterShift | None"] = relationship("CounterShift")
    counter: Mapped["Counter"] = relationship("Counter")
    employee: Mapped["Employee"] = relationship("Employee")

    def __repr__(self) -> str:
        return f"<CashSession id={self.id} org={self.organization_id} status={self.status}>"


# ── PAY-003..005: Refunds ─────────────────────────────────────────────────────

class RefundStatus(str, enum.Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    PROCESSED = "processed"
    REJECTED = "rejected"
    FAILED = "failed"


class Refund(Base, TimestampMixin):
    """
    Idempotent refund tracking for online and cash payments.
    """
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    token_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="SET NULL"), nullable=True, index=True
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="INR")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=RefundStatus.PENDING_APPROVAL.value, index=True
    )

    gateway_refund_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True, index=True)

    requested_by_type: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_by_id: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_by_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization")
    payment: Mapped["Payment"] = relationship("Payment")
    token: Mapped["Token | None"] = relationship("Token")

    def __repr__(self) -> str:
        return f"<Refund id={self.id} payment_id={self.payment_id} amount={self.amount} status={self.status}>"


# ── Settlements & Payouts ─────────────────────────────────────────────────────

class PayoutStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    PAID = "paid"
    HELD = "held"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Payout(Base, TimestampMixin):
    """
    Scheduled or disbursed organization payout.
    Can be held/released by authorized finance admins.
    """
    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="INR")
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PayoutStatus.SCHEDULED.value, index=True
    )

    hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    held_by_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    held_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    bank_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gateway_payout_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")

    def __repr__(self) -> str:
        return f"<Payout id={self.id} org={self.organization_id} amount={self.amount} status={self.status}>"


class Settlement(Base):
    """
    Gateway settlement report linking gross charges, fees, taxes and net payouts.
    """
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payout_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payouts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    gateway_settlement_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    gross_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    net_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    settlement_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    organization: Mapped["Organization"] = relationship("Organization")
    payout: Mapped["Payout | None"] = relationship("Payout")


# ── FIN-002: Organization Bank Account & Verification Hold ────────────────────

class BankAccountStatus(str, enum.Enum):
    PENDING_VERIFICATION = "pending_verification"
    VERIFIED = "verified"
    HELD = "held"
    REJECTED = "rejected"


class OrgBankAccount(Base, TimestampMixin):
    """
    Bank account details for payout processing.
    Modifications enforce temporary payout hold (e.g. 72h) and owner notification.
    """
    __tablename__ = "org_bank_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_number: Mapped[str] = mapped_column(String(50), nullable=False)
    ifsc_code: Mapped[str] = mapped_column(String(20), nullable=False)
    account_holder_name: Mapped[str] = mapped_column(String(150), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=BankAccountStatus.PENDING_VERIFICATION.value, index=True
    )

    verification_token: Mapped[str | None] = mapped_column(String(100), nullable=True)
    held_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")

    def __repr__(self) -> str:
        return f"<OrgBankAccount id={self.id} org={self.organization_id} status={self.status}>"


# ── FIN-003..004: Proportional Risk Rules & Flags ────────────────────────────

class RiskSeverity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskFlagStatus(str, enum.Enum):
    FLAGGED = "flagged"
    UNDER_REVIEW = "under_review"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class FinancialRiskFlag(Base, TimestampMixin):
    """
    Proportional risk flags (FIN-003/004) evaluating transactions, shifts,
    refund spikes, cash discrepancies, and pricing changes without permanent punitive lockout.
    """
    __tablename__ = "financial_risk_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    risk_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default=RiskSeverity.MEDIUM.value)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=RiskFlagStatus.FLAGGED.value, index=True
    )

    description: Mapped[str] = mapped_column(Text, nullable=False)
    flag_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")

    def __repr__(self) -> str:
        return f"<FinancialRiskFlag id={self.id} type={self.risk_type} severity={self.severity} status={self.status}>"


# ── Webhook Replay Protection & Idempotency ──────────────────────────────────

class WebhookEvent(Base):
    """
    Replay-safe webhook deduplication ledger (PAY-003).
    """
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="processed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<WebhookEvent id={self.id} event_id={self.event_id} status={self.status}>"

