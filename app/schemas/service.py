"""app/schemas/service.py"""

from decimal import Decimal
from typing import Optional
from pydantic import BaseModel


class ServiceCreate(BaseModel):
    name: str
    description: Optional[str] = None
    price: Decimal = Decimal("0.00")
    estimated_minutes: int = 10


class ServiceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[Decimal] = None
    estimated_minutes: Optional[int] = None
    is_active: Optional[bool] = None


class ServiceRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    organization_id: int
    name: str
    description: Optional[str]
    price: Decimal
    estimated_minutes: int
    is_active: bool
