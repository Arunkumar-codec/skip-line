"""
app/services/session_service.py
───────────────────────────────
Server-side session management for authenticated principals.
Handles creation, validation, activity updates, and revocation.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from models.session import Session


async def create_session(
    db: AsyncSession,
    principal_type: str,
    principal_id: int,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    expires_in_minutes: Optional[int] = None,
) -> Session:
    """Create and persist a new authenticated session."""
    session_id = str(uuid.uuid4())
    expiry_minutes = expires_in_minutes or settings.jwt_access_token_expire_minutes
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=expiry_minutes)

    session = Session(
        id=session_id,
        principal_type=principal_type,
        principal_id=principal_id,
        ip_address=ip_address,
        user_agent=user_agent[:500] if user_agent else None,
        is_revoked=False,
        expires_at=expires_at,
        created_at=now,
        last_activity_at=now,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_valid_session(db: AsyncSession, session_id: str) -> Optional[Session]:
    """Retrieve an active, non-revoked, non-expired session."""
    session = await db.get(Session, session_id)
    if not session or session.is_revoked:
        return None

    now = datetime.now(timezone.utc)
    # Ensure timezone-aware comparison
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at <= now:
        return None

    session.last_activity_at = now
    await db.commit()
    return session


async def revoke_session(db: AsyncSession, session_id: str) -> bool:
    """Revoke a specific session immediately."""
    session = await db.get(Session, session_id)
    if not session:
        return False
    session.is_revoked = True
    await db.commit()
    return True


async def revoke_all_sessions(
    db: AsyncSession, principal_type: str, principal_id: int
) -> int:
    """Revoke all active sessions for a principal across all devices."""
    result = await db.execute(
        update(Session)
        .where(
            Session.principal_type == principal_type,
            Session.principal_id == principal_id,
            Session.is_revoked == False,  # noqa: E712
        )
        .values(is_revoked=True)
    )
    await db.commit()
    return result.rowcount


async def get_active_sessions_for_principal(
    db: AsyncSession, principal_type: str, principal_id: int
) -> list[Session]:
    """Retrieve all non-revoked sessions for a principal."""
    result = await db.execute(
        select(Session)
        .where(
            Session.principal_type == principal_type,
            Session.principal_id == principal_id,
            Session.is_revoked == False,  # noqa: E712
        )
        .order_by(Session.created_at.desc())
    )
    return list(result.scalars().all())
