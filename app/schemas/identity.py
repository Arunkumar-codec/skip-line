"""app/schemas/identity.py"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class IdentitySubmitRequest(BaseModel):
    id_type: str = Field(..., max_length=50)  # national_id, passport, driving_license, aadhaar, voters_id, other
    id_number: str = Field(..., min_length=1, max_length=100)
    organization_id: Optional[int] = None


class IdentityReviewRequest(BaseModel):
    action: str = Field(..., max_length=20)  # approve | reject
    rejection_reason: Optional[str] = None
    expires_at: Optional[datetime] = None


class UserIdentityRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    user_id: int
    organization_id: Optional[int]
    id_type: str
    masked_id_number: str
    document_path: Optional[str]
    status: str
    rejection_reason: Optional[str]
    reviewed_at: Optional[datetime]
    expires_at: Optional[datetime]
    created_at: datetime
