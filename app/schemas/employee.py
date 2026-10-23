"""
app/schemas/employee.py
───────────────────────
Pydantic schemas for employee accounts and authentication.
Never exposes password_hash in response models.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class EmployeeBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=120, description="Employee full name")
    email: EmailStr = Field(..., description="Unique employee email address")
    employee_code: str = Field(..., min_length=1, max_length=50, description="Unique code within organization")
    branch_id: int = Field(..., description="ID of assigned branch")


class EmployeeCreate(EmployeeBase):
    password: str = Field(..., min_length=6, max_length=128, description="Initial employee password")
    is_active: bool = Field(True, description="Account active status")


class EmployeeUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    email: Optional[EmailStr] = None
    employee_code: Optional[str] = Field(None, min_length=1, max_length=50)
    branch_id: Optional[int] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=6, max_length=128)


class EmployeeRead(EmployeeBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    branch_id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class EmployeeLoginRequest(BaseModel):
    email: EmailStr
    password: str


class EmployeeLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str = "employee"
    entity_id: int
    organization_id: int
    branch_id: int
    name: str
    email: str
