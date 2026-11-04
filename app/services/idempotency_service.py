"""
app/services/idempotency_service.py
───────────────────────────────────
Phase 8 — Durable idempotency for token/queue mutations (DB-001).

API clients send `Idempotency-Key`; `begin()` get-or-inserts a durable
row under the UNIQUE (organization_id, key) constraint:
  - NEW  → caller runs the operation, then records the result via
           `complete()`
  - COMPLETED existing row → caller replays the recorded result
  - OPEN existing row → a retry of the same operation is currently in
    flight; the caller must reject with a conflict

Money paths (payments/refunds/webhooks) retain their existing
state-check idempotency; kiosk/walk-in retries keep the channel-level
`tokens.client_reference` mechanism.
"""

import hashlib
import json
from typing import Any, Optional

from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.idempotency import IdempotencyKey, IdempotencyState


def fingerprint_payload(payload: Any) -> str:
    """Stable SHA-256 over the canonical request payload (used to detect
    a retry that sends DIFFERENT data under the same idempotency key)."""
    if isinstance(payload, dict):
        payload = {
            k: payload[k] for k in sorted(payload)
            if payload[k] is not None
        }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _fetch(
    db: AsyncSession, organization_id: int, key: str
) -> Optional[IdempotencyKey]:
    return (
        await db.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.organization_id == organization_id,
                IdempotencyKey.key == key,
            )
        )
    ).scalar_one_or_none()


async def begin(
    db: AsyncSession,
    *,
    organization_id: int,
    key: str,
    operation: str,
    payload: Any = None,
    principal_type: Optional[str] = None,
    principal_id: Optional[int] = None,
) -> tuple[IdempotencyKey, bool]:
    """Start (or join) the idempotent operation.

    Returns (row, freshly_acquired):
      - fresh/failure-retry acquisition → (OPEN row, True): the caller
        proceeds and MUST ``complete()`` (or ``fail()``) at the end;
      - join attempt → (existing row, False): the caller replays when the
        row is COMPLETED or raises a conflict when it is still OPEN.
    """
    clean = (key or "").strip()
    if not clean:
        raise ValueError("Idempotency key cannot be empty")
    if len(clean) > 255:
        raise ValueError("Idempotency key is too long")

    existing = await _fetch(db, organization_id, clean)
    if existing is not None:
        if existing.status == IdempotencyState.FAILED:
            # the previous attempt recorded failure — the key is reusable
            # by design (fail()): reset to OPEN for this fresh attempt
            existing.status = IdempotencyState.OPEN
            await db.flush()
            return existing, True  # re-acquired
        return existing, False

    row = IdempotencyKey(
        organization_id=organization_id,
        key=clean,
        operation=operation,
        fingerprint=fingerprint_payload(payload) if payload is not None else None,
        status=IdempotencyState.OPEN,
        principal_type=principal_type,
        principal_id=principal_id,
    )
    db.add(row)
    try:
        await db.flush()
        return row, True  # freshly acquired
    except IntegrityError:
        # A concurrent request already holds this (organization_id, key).
        # The duplicate insert violates the unique constraint; plain
        # rollback at this point is safe because `begin()` runs before
        # the caller performs any writes.
        await db.rollback()
        existing = await _fetch(db, organization_id, clean)
        if existing is not None:
            return existing, False
        logger.warning(
            "Idempotency race on org={} key={} — no committed row visible",
            organization_id, clean,
        )
        raise ValueError(
            "idempotency conflict: a concurrent request is processing this key"
        )


async def complete(
    db: AsyncSession,
    row: IdempotencyKey,
    response_snapshot: dict,
    *,
    commit: bool = True,
) -> None:
    """Record a successful result for replay. Never overwrites a row
    that already reached a terminal state."""
    if row.status not in (IdempotencyState.OPEN,):
        logger.warning(
            "Idempotency row {} in state {}; result not recorded again",
            row.id, row.status,
        )
        return
    row.status = IdempotencyState.COMPLETED
    row.response_snapshot = response_snapshot
    if commit:
        await db.commit()


async def fail(
    db: AsyncSession,
    row: IdempotencyKey,
    *,
    commit: bool = True,
) -> None:
    """Record a failed attempt; the key stays reusable by design (the row
    transitions to FAILED and callers may treat it as absent)."""
    row.status = IdempotencyState.FAILED
    if commit:
        await db.commit()


async def replay_token(
    db: AsyncSession, row: IdempotencyKey
) -> Optional["Token"]:  # Token imported lazily below (avoids import cycle)
    """Fetch the recorded token for a COMPLETED row (any channel)."""
    from models.token import Token

    snapshot = row.response_snapshot or {}
    token_id = snapshot.get("token_id")
    if not token_id:
        return None
    return (await db.execute(select(Token).where(
        and_(Token.id == token_id, Token.organization_id == row.organization_id)
    ))).scalar_one_or_none()
