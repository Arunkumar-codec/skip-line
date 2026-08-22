"""app/schemas/auth.py — authentication request and response schemas."""

from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    entity_id: int
    name: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str
