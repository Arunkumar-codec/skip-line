"""app/schemas/common.py — shared Pydantic response envelopes."""

from typing import Any, Generic, Optional, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    success: bool = True
    message: str = "OK"
    data: Optional[T] = None


class PaginatedResponse(BaseModel, Generic[T]):
    success: bool = True
    total: int
    page: int
    per_page: int
    pages: int
    data: list[T]


class MessageResponse(BaseModel):
    success: bool = True
    message: str
