"""app/schemas/payment.py"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel


class PaymentInitRequest(BaseModel):
    token_id: int


class PaymentVerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class PaymentRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    token_id: int
    amount: Decimal
    currency: str
    payment_method: str
    status: str
    razorpay_order_id: Optional[str]
    razorpay_payment_id: Optional[str]
    paid_at: Optional[datetime]
    created_at: datetime
