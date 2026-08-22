"""app/schemas/user.py"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator


class UserRegisterRequest(BaseModel):
    name: str
    email: EmailStr
    mobile: str
    address: Optional[str] = None
    password: str
    confirm_password: str

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v, info):
        if "password" in info.data and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v


class UserUpdateRequest(BaseModel):
    name: Optional[str] = None
    mobile: Optional[str] = None
    address: Optional[str] = None


class UserRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    email: str
    mobile: str
    address: Optional[str]
    is_active: bool
    profile_photo: Optional[str]
    created_at: datetime
