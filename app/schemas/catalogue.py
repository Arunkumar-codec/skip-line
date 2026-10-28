"""app/schemas/catalogue.py"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field


class SubServiceCreate(BaseModel):
    service_id: Optional[int] = None
    name: str = Field(..., max_length=200)
    code: str = Field(..., max_length=50)
    description: Optional[str] = None
    additional_price: Decimal = Decimal("0.00")
    additional_minutes: int = 0
    is_active: bool = True
    display_order: int = 0


class SubServiceUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    code: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = None
    additional_price: Optional[Decimal] = None
    additional_minutes: Optional[int] = None
    is_active: Optional[bool] = None
    display_order: Optional[int] = None


class SubServiceRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    service_id: int
    name: str
    code: str
    description: Optional[str]
    additional_price: Decimal
    additional_minutes: int
    is_active: bool
    display_order: int
    created_at: datetime


class ServiceRollup(BaseModel):
    service_id: int
    service_name: str
    base_price: float
    base_minutes: int
    total_price: float
    total_minutes: int
    sub_services_count: int


class ServiceDetailRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    organization_id: int
    branch_id: Optional[int]
    name: str
    service_code: Optional[str]
    description: Optional[str]
    price: Decimal
    estimated_minutes: int
    is_active: bool
    requires_identity_verification: bool
    sub_services: list[SubServiceRead] = []
    created_at: datetime


class CounterServicesAssignRequest(BaseModel):
    service_ids: list[int]
    primary_service_id: Optional[int] = None


class CounterServiceItemRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    service_id: int
    name: str
    is_primary: bool
