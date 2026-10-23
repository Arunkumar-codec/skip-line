"""
app/schemas/branch.py
─────────────────────
Pydantic schemas for physical organization branches.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class BranchBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=200, description="Branch display name")
    branch_code: str = Field(..., min_length=1, max_length=50, description="Unique code within organization")
    address: str = Field(..., min_length=3, description="Street address")
    city: str = Field(..., min_length=2, max_length=100)
    state: str = Field(..., min_length=2, max_length=100)
    pincode: str = Field(..., min_length=4, max_length=20)


class BranchCreate(BranchBase):
    is_active: bool = True


class BranchUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=200)
    branch_code: Optional[str] = Field(None, min_length=1, max_length=50)
    address: Optional[str] = Field(None, min_length=3)
    city: Optional[str] = Field(None, min_length=2, max_length=100)
    state: Optional[str] = Field(None, min_length=2, max_length=100)
    pincode: Optional[str] = Field(None, min_length=4, max_length=20)
    is_active: Optional[bool] = None


class BranchRead(BranchBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
