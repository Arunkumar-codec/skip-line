"""
models/payment.py
─────────────────
Payment records linked to tokens.
Supports Razorpay (UPI, card, netbanking) and manual cash payments.
"""

import enum
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from models.mixins import TimestampMixin


class PaymentMethod(str, enum.Enum):
    RAZORPAY = "razorpay"
    UPI = "upi"
    CASH = "cash"
    WAIVED = "waived"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


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

    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="INR")

    payment_method: Mapped[str] = mapped_column(
        Enum(PaymentMethod), nullable=False, default=PaymentMethod.RAZORPAY
    )
    status: Mapped[str] = mapped_column(
        Enum(PaymentStatus), nullable=False, default=PaymentStatus.PENDING, index=True
    )

    # Razorpay identifiers
    razorpay_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    razorpay_signature: Mapped[str | None] = mapped_column(String(500), nullable=True)

    paid_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipt_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Relationships
    token: Mapped["Token"] = relationship("Token", back_populates="payments")
    user: Mapped["User | None"] = relationship("User", back_populates="payments")
    organization: Mapped["Organization"] = relationship("Organization", back_populates="payments")

    def __repr__(self) -> str:
        return f"<Payment id={self.id} amount={self.amount} status={self.status}>"
