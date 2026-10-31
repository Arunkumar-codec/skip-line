"""
models/payment.py
─────────────────
Payment records linked to tokens.
Supports Razorpay (UPI, card, netbanking) and manual cash payments.
"""

import enum
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class PaymentMethod(str, enum.Enum):
    RAZORPAY = "razorpay"
    UPI = "upi"
    CASH = "cash"
    WAIVED = "waived"
    FREE = "free"
    PAY_AT_COUNTER = "pay_at_counter"


class PaymentStatus(str, enum.Enum):
    INITIATED = "initiated"
    PENDING = "pending"
    SUCCESSFUL = "successful"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNDER_REVIEW = "under_review"
    DISPUTED = "disputed"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    CASH_COLLECTED = "cash_collected"
    CASH_PENDING = "cash_pending"
    PAY_AT_COUNTER = "pay_at_counter"
    WAIVED = "waived"
    FREE = "free"


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    counter_shift_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("counter_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="INR")

    payment_method: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PaymentMethod.RAZORPAY.value
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PaymentStatus.PENDING.value, index=True
    )

    # Actor & waiver details (PHY-001..004)
    recorded_by_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recorded_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    waiver_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    waiver_approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # PHY-003: supporting evidence reference recorded with supervised waivers.
    waiver_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Razorpay identifiers
    razorpay_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    razorpay_signature: Mapped[str | None] = mapped_column(String(500), nullable=True)

    paid_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disputed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipt_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Relationships
    token: Mapped["Token"] = relationship("Token", back_populates="payments")
    user: Mapped["User | None"] = relationship("User", back_populates="payments")
    organization: Mapped["Organization"] = relationship("Organization", back_populates="payments")
    counter_shift: Mapped["CounterShift | None"] = relationship("CounterShift")

    def __repr__(self) -> str:
        return f"<Payment id={self.id} amount={self.amount} status={self.status}>"
