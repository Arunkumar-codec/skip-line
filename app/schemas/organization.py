"""app/schemas/organization.py"""

from datetime import date, datetime, time
from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator
from models.organization import OrgStatus, OrgType


class OrgRegisterRequest(BaseModel):
    name: str
    registration_number: str
    org_type: OrgType
    address: str
    city: str
    state: str
    email: EmailStr
    mobile: str
    password: str
    confirm_password: str
    description: Optional[str] = None

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v, info):
        if "password" in info.data and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v

    @field_validator("mobile")
    @classmethod
    def mobile_digits(cls, v):
        if not v.replace("+", "").replace(" ", "").isdigit():
            raise ValueError("Mobile must contain only digits")
        return v


class OrgUpdateRequest(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    mobile: Optional[str] = None
    description: Optional[str] = None


class OrgRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    registration_number: str
    org_type: str
    address: str
    city: str
    state: str
    email: str
    mobile: str
    status: str
    logo_path: Optional[str]
    description: Optional[str]
    is_active: bool
    approved_at: Optional[datetime]
    created_at: datetime


class OrgSearchResult(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    org_type: str
    city: str
    state: str
    description: Optional[str]
    logo_path: Optional[str]
    # Distance in kilometres to the organization's nearest geocoded active
    # branch — populated only when the caller supplied lat/lng coordinates
    # (Phase 3 Task 4 distance search); None otherwise.
    distance_km: Optional[float] = None


class SlotDateRead(BaseModel):
    """Public per-date bookability summary (Phase 3 Task 4 discovery)."""

    model_config = {"from_attributes": True}

    date: date
    state: str
    remaining: Optional[int]
    opens_at: Optional[time]
    closes_at: Optional[time]
    timezone_name: str
    is_legacy_day_queue: bool


class PublicSlotRead(BaseModel):
    """One bookable slot occurrence, SCH-009-rendered in the tenant
    timezone: `start_local`/`end_local` are wall-clock times in
    `timezone_name`; `start_utc`/`end_utc` are absolute UTC instants."""

    model_config = {"from_attributes": True}

    id: int
    counter_id: int
    counter_number: int
    counter_name: str
    slot_date: date
    start_local: time
    end_local: time
    start_utc: datetime
    end_utc: datetime
    capacity: int
    remaining: int
    is_bookable: bool


class PublicSlotDayRead(BaseModel):
    """Per-date public slot ladder (SCH-005/SCH-009)."""

    model_config = {"from_attributes": True}

    date: date
    state: str
    reason: Optional[str] = None
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    timezone_name: str
    is_legacy_day_queue: bool
    slots: list[PublicSlotRead] = []
