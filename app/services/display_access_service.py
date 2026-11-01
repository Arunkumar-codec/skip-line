"""
app/services/display_access_service.py
──────────────────────────────────────
Public TV display authorization (Decision 20 — signed display-access-key).

A public display is a read-only registered-ish device: it must not receive
cross-tenant data and must not be able to discover other organizations'
queues simply by enumerating org ids on /public/display/{org_id}.

An organization head rotates an access key once per screen installation;
the plaintext is shown exactly once at rotation time and only its SHA-256
hash is stored (HTTP-hashed semantics, same as password practice but at
rest disclosure would not compromise existing keys without the hash being
crackable only offline... which is why we store the hash). Verification is
constant-time (hmac.compare_digest on digests).

Configuration lives in the existing per-org `settings` table — no schema
change required for this part of Decision 20 (see OPEN_DECISIONS).
"""

import hashlib
import hmac
import secrets
import time
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from models.setting import Setting

DISPLAY_KEY_SETTING = "display_access_key"       # SHA-256 hex of installed key
DISPLAY_KEY_ROTATED_AT_SETTING = "display_access_key_rotated_at"


async def install_or_rotate_key(db: AsyncSession, org_id: int) -> str:
    """Generate a new key, store its hash, return the plaintext (never stored)."""
    plaintext = secrets.token_urlsafe(24)
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    await _upsert_setting(db, org_id, DISPLAY_KEY_SETTING, digest)
    await _upsert_setting(db, org_id, DISPLAY_KEY_ROTATED_AT_SETTING, str(int(time.time())))
    await db.commit()
    logger.info("Display access key rotated for org {}", org_id)
    return plaintext


async def revoke_key(db: AsyncSession, org_id: int) -> None:
    await _upsert_setting(db, org_id, DISPLAY_KEY_SETTING, None)
    await db.commit()


async def has_key_installed(db: AsyncSession, org_id: int) -> bool:
    return bool(await _hash_for_org(db, org_id))


async def is_key_installed(db: AsyncSession, org_id: int, provided: str) -> bool:
    """Constant-time comparison of a provided key against the stored hash."""
    stored = await _hash_for_org(db, org_id)
    if not stored or not provided or not isinstance(provided, str):
        return False
    provided_digest = hashlib.sha256(provided.encode()).hexdigest()
    return hmac.compare_digest(provided_digest, stored)


async def _hash_for_org(db: AsyncSession, org_id: int) -> Optional[str]:
    from sqlalchemy import select

    result = await db.execute(
        select(Setting.value).where(
            Setting.organization_id == org_id, Setting.key == DISPLAY_KEY_SETTING
        )
    )
    value = result.scalar_one_or_none()
    return value or None


async def _upsert_setting(
    db: AsyncSession, org_id: int, key: str, value: Optional[str]
) -> None:
    from sqlalchemy import select

    result = await db.execute(
        select(Setting).where(
            Setting.organization_id == org_id, Setting.key == key
        )
    )
    setting = result.scalar_one_or_none()
    if setting is None:
        db.add(Setting(organization_id=org_id, key=key, value=value))
    else:
        setting.value = value
    await db.flush()
