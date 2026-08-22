"""app/schemas/organization.py"""

from datetime import datetime
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
