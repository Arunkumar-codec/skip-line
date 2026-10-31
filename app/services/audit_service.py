"""
app/services/audit_service.py
─────────────────────────────
Append-only audit service for logging security, administrative, and permission events.
Guarantees sensitive credentials (passwords, tokens, secrets) are NEVER persisted.
"""

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.audit_log import AuditLog

# Keys that must never appear in audit details
SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "confirm_password",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "secret_key",
    "credentials",
    "jwt",
}


def _sanitize_details(details: Optional[dict[str, Any]]) -> Optional[str]:
    """Strip all sensitive fields before persisting audit details."""
    if not details:
        return None

    clean = {}
    for key, val in details.items():
        if key.lower() in SENSITIVE_KEYS:
            continue
        # Convert non-serializable objects to string representation
        if isinstance(val, (int, float, bool, str, list, dict)) or val is None:
            clean[key] = val
        else:
            clean[key] = str(val)

    return json.dumps(clean)


async def log_audit_event(
    db: AsyncSession,
    action: str,
    actor_type: str,
    actor_id: Optional[int] = None,
    actor_email: Optional[str] = None,
    organization_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    outcome: str = "success",
    details: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    commit: bool = True,
) -> AuditLog:
    """Record an immutable audit event."""
    log_entry = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        actor_email=actor_email,
        organization_id=organization_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        outcome=outcome,
        ip_address=ip_address,
        details=_sanitize_details(details),
        created_at=datetime.now(timezone.utc),
    )
    db.add(log_entry)
    if commit:
        await db.commit()
        await db.refresh(log_entry)
    return log_entry


async def list_audit_logs(
    db: AsyncSession,
    organization_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditLog]:
    """Retrieve audit logs with optional organization tenant scoping."""
    query = select(AuditLog)
    if organization_id is not None:
        query = query.where(AuditLog.organization_id == organization_id)
    query = query.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    return list(result.scalars().all())
