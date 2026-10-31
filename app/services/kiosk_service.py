"""
app/services/kiosk_service.py
─────────────────────────────
Kiosk device management, pairing flow, heartbeat tracking, and revocation (KSK-001..006).

Pairing flow (Open Decision 7):
1. Kiosk device requests pairing -> receives 6-digit numeric PIN with 15-minute TTL.
2. Org Head confirms PIN in Org dashboard -> assigns branch/services.
3. Kiosk device exchanges PIN for device API key / persistent access credential.
4. Org Head can revoke device at any time.
"""

import hashlib
import random
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.branch import Branch
from models.kiosk_device import KioskDevice


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _generate_pin() -> str:
    return f"{random.randint(100000, 999999)}"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def list_kiosks(
    db: AsyncSession,
    organization_id: int,
) -> list[KioskDevice]:
    stmt = (
        select(KioskDevice)
        .where(KioskDevice.organization_id == organization_id)
        .order_by(KioskDevice.created_at.desc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def request_pairing_pin(
    db: AsyncSession,
    device_code: str,
    device_name: Optional[str] = None,
) -> str:
    """Device requests a 6-digit pairing PIN (KSK-001 / Open Decision 7)."""
    stmt = select(KioskDevice).where(KioskDevice.device_code == device_code)
    device = (await db.execute(stmt)).scalar_one_or_none()

    pin = _generate_pin()
    expires = _utcnow() + timedelta(minutes=15)

    if device:
        device.pairing_code = pin
        device.pairing_code_expires_at = expires
        if device_name:
            device.device_name = device_name
    else:
        # Create unassociated / pending device entry (organization_id 0 placeholder until confirmed)
        device = KioskDevice(
            organization_id=1,  # Temporary, reassigned upon head confirmation
            device_name=device_name or f"Kiosk-{device_code[:6]}",
            device_code=device_code,
            pairing_code=pin,
            pairing_code_expires_at=expires,
            is_active=False,
        )
        db.add(device)

    await db.commit()
    await db.refresh(device)
    return pin


async def confirm_pairing_pin(
    db: AsyncSession,
    organization_id: int,
    pairing_code: str,
    device_name: Optional[str] = None,
    branch_id: Optional[int] = None,
) -> KioskDevice:
    """Org Head enters 6-digit PIN in dashboard to activate device."""
    now = _utcnow()
    stmt = select(KioskDevice).where(
        and_(
            KioskDevice.pairing_code == pairing_code.strip(),
            KioskDevice.pairing_code_expires_at > now,
        )
    )
    device = (await db.execute(stmt)).scalar_one_or_none()
    if not device:
        raise ValueError("Invalid or expired pairing code")

    if branch_id:
        branch = await db.get(Branch, branch_id)
        if not branch or branch.organization_id != organization_id:
            raise ValueError("Branch not found")
        device.branch_id = branch_id

    device.organization_id = organization_id
    if device_name:
        device.device_name = device_name
    device.is_active = True
    device.pairing_code_expires_at = now + timedelta(minutes=5)  # grace period for exchange

    await db.commit()
    await db.refresh(device)
    return device


async def exchange_pairing_pin(
    db: AsyncSession,
    device_code: str,
    pairing_code: str,
) -> tuple[KioskDevice, str]:
    """Kiosk exchanges confirmed PIN for persistent device API key."""
    now = _utcnow()
    stmt = select(KioskDevice).where(
        and_(
            KioskDevice.device_code == device_code,
            KioskDevice.pairing_code == pairing_code.strip(),
            KioskDevice.is_active == True,  # must be approved by head
            KioskDevice.pairing_code_expires_at > now,
        )
    )
    device = (await db.execute(stmt)).scalar_one_or_none()
    if not device:
        raise ValueError("Device is not paired or pairing code expired")

    raw_api_key = f"kiosk_{secrets.token_urlsafe(32)}"
    device.api_key_hash = _hash_key(raw_api_key)
    device.pairing_code = None
    device.pairing_code_expires_at = None
    device.last_heartbeat_at = now

    await db.commit()
    await db.refresh(device)
    return device, raw_api_key


async def record_heartbeat(
    db: AsyncSession,
    device_code: str,
) -> KioskDevice:
    """Record kiosk heartbeat (KSK-006)."""
    stmt = select(KioskDevice).where(KioskDevice.device_code == device_code)
    device = (await db.execute(stmt)).scalar_one_or_none()
    if not device or not device.is_active:
        raise ValueError("Active kiosk device not found")

    device.last_heartbeat_at = _utcnow()
    await db.commit()
    return device


async def revoke_kiosk(
    db: AsyncSession,
    organization_id: int,
    kiosk_id: int,
) -> KioskDevice:
    """Org head revokes a kiosk device."""
    device = await db.get(KioskDevice, kiosk_id)
    if not device or device.organization_id != organization_id:
        raise ValueError("Kiosk device not found")

    device.is_active = False
    device.api_key_hash = None
    await db.commit()
    await db.refresh(device)
    return device

