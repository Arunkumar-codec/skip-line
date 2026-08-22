"""app/schemas/counter.py"""

from typing import Optional
from pydantic import BaseModel
from models.counter import CounterStatus


class CounterCreate(BaseModel):
    counter_name: str
    counter_number: int
    token_prefix: str = "A"
    daily_limit: int = 200
    service_id: Optional[int] = None
    status: CounterStatus = CounterStatus.ACTIVE


class CounterUpdate(BaseModel):
    counter_name: Optional[str] = None
    token_prefix: Optional[str] = None
    daily_limit: Optional[int] = None
    service_id: Optional[int] = None
    status: Optional[CounterStatus] = None


class CounterRead(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    organization_id: int
    counter_name: str
    counter_number: int
    token_prefix: str
    daily_limit: int
    status: str
    service_id: Optional[int]
