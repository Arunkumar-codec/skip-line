"""
app/utils/jwt.py
────────────────
JWT helpers for creating and decoding access tokens.
All tokens carry: sub (entity id), role, and exp.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from loguru import logger

from app.config import settings


def create_access_token(
    subject: int,
    role: str,
    expires_delta: Optional[timedelta] = None,
    extra_claims: Optional[dict] = None,
) -> str:
    """
    Create a signed JWT.

    Args:
        subject: Database ID of the authenticated entity (admin, org, user, or employee).
        role: One of "admin" | "organization" | "user" | "employee".
        expires_delta: Override the default expiry window.
        extra_claims: Optional additional claims to include in the payload.

    Returns:
        Signed JWT string.
    """
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    payload = {
        "sub": str(subject),
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Optional[dict]:
    """
    Decode and verify a JWT.

    Returns:
        The decoded payload dict, or None if the token is invalid/expired.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        return payload
    except JWTError as exc:
        logger.debug("JWT decode failed: {}", exc)
        return None
