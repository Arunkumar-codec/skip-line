"""app/schemas/token.py"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from pydantic import BaseModel, Field
from models.token import TokenStatus


class TokenBookRequest(BaseModel):
    """Authenticated user booking a token."""
    organization_id: int
    counter_id: Optional[int] = None
    service_id: Optional[int] = None
    sub_service_ids: Optional[list[int]] = None
    form_answers: Optional[dict[str, Any]] = None
    # Appointment slot for slot-mode counters (Decision 10); NULL = day-queue
    slot_id: Optional[int] = None
    client_reference: Optional[str] = None


class KioskTokenRequest(BaseModel):
    """Guest booking from a kiosk — no login required."""
    organization_id: int
    counter_id: Optional[int] = None
    service_id: Optional[int] = None
    sub_service_ids: Optional[list[int]] = None
    form_answers: Optional[dict[str, Any]] = None
    guest_name: str
    guest_mobile: str
    # Appointment slot for slot-mode counters (Decision 10); NULL = day-queue
    slot_id: Optional[int] = None
    client_reference: Optional[str] = None


class TokenActionRequest(BaseModel):
    """Organization operator performing an action on a token."""
    action: str   # call | complete | skip | cancel | hold | recall
    notes: Optional[str] = None


class TokenScanRequest(BaseModel):
    """Authorized scanner check-in (QR-003).

    `ref` is the HMAC-signed opaque reference decoded from the QR image;
    `counter_id` is the desk counter the scanner operates (must hold an
    active approved shift); `device_id` optionally names the scanner device
    for the QR-005 audit trail.
    """
    ref: str = Field(min_length=8, max_length=512)
    counter_id: Optional[int] = None
    device_id: Optional[str] = None


class TokenRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    organization_id: int
    user_id: Optional[int]
    counter_id: int
    service_id: Optional[int]
    slot_id: Optional[int]
    token_number: str
    status: str
    queue_position: int
    booking_source: Optional[str] = None
    guest_name: Optional[str]
    guest_mobile: Optional[str]
    qr_code_path: Optional[str]
    receipt_path: Optional[str]
    amount_paid: Decimal
    booked_at: Optional[datetime]
    called_at: Optional[datetime]
    completed_at: Optional[datetime]
    checked_in_at: Optional[datetime] = None
    created_at: datetime


class TokenTrackingResponse(BaseModel):
    """Live tracking payload sent to user and via WebSocket."""
    token_id: int
    token_number: str
    status: str
    queue_position: int
    people_ahead: int
    estimated_wait_minutes: int
    current_token_at_counter: Optional[str]
    counter_name: str
