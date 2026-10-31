"""
app/schemas/kiosk.py
────────────────────
Pydantic schemas for self-service kiosk devices (KSK-001..006).
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import APIResponse


class KioskDeviceCreate(BaseModel):
    device_name: str = Field(..., min_length=2, max_length=100)
    device_code: str = Field(..., min_length=2, max_length=50)
    branch_id: Optional[int] = None
    allowed_services: Optional[list[int]] = None
    allowed_counters: Optional[list[int]] = None


class KioskPairRequest(BaseModel):
    device_code: str = Field(..., min_length=2, max_length=50)
    device_name: Optional[str] = None


class KioskPairConfirmRequest(BaseModel):
    pairing_code: str = Field(..., min_length=6, max_length=10)
    device_name: Optional[str] = None
    branch_id: Optional[int] = None


class KioskPairExchangeRequest(BaseModel):
    device_code: str = Field(..., min_length=2, max_length=50)
    pairing_code: str = Field(..., min_length=6, max_length=10)


class KioskHeartbeatRequest(BaseModel):
    device_code: str = Field(..., min_length=2, max_length=50)


class KioskDeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    branch_id: Optional[int] = None
    device_name: str
    device_code: str
    is_active: bool
    last_heartbeat_at: Optional[datetime] = None
    allowed_services: Optional[list[int]] = None
    allowed_counters: Optional[list[int]] = None
    created_at: datetime
    updated_at: datetime


KioskDeviceListResponse = APIResponse[list[KioskDeviceRead]]
KioskDeviceResponse = APIResponse[KioskDeviceRead]

