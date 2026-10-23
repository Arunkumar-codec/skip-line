"""
app/schemas/rbac.py
───────────────────
Pydantic schemas for Roles, Permissions, and Role Assignments.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class PermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: Optional[str] = None
    module: str


class RoleBase(BaseModel):
    code: str = Field(..., min_length=2, max_length=50)
    name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = None


class RoleRead(RoleBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: Optional[int] = None
    is_system: bool
    permissions: list[PermissionRead] = []
    created_at: datetime


class RoleAssignRequest(BaseModel):
    role_id: int


class RoleAssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    role_id: int
    created_at: datetime
