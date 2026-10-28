"""
app/services/identity_service.py
────────────────────────────────
Customer Identity Verification (KYC) management.
Handles:
- AES encryption/decryption of sensitive government ID numbers at rest (Fernet)
- Display masking (e.g. ••••1234)
- Document upload and unguessable filename storage
- KYC review desk (approve / reject)
- Booking gating verification checks
"""

import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import uuid

from cryptography.fernet import Fernet
from fastapi import HTTPException, status
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from models.user_identity import UserIdentity


def _get_fernet() -> Fernet:
    """Derive a URL-safe 32-byte Fernet key deterministically from settings.secret_key."""
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def encrypt_id_number(raw_id: str) -> str:
    """Encrypt a raw government ID number using Fernet AES."""
    if not raw_id:
        raise ValueError("ID number cannot be empty")
    return _get_fernet().encrypt(raw_id.strip().encode()).decode()


def decrypt_id_number(cipher_text: str) -> str:
    """Decrypt an encrypted government ID number."""
    if not cipher_text:
        return ""
    return _get_fernet().decrypt(cipher_text.strip().encode()).decode()


def mask_id_number(raw_id: str) -> str:
    """
    Produce a secure masked representation of a government ID number.
    Preserves the last 4 characters; masks all preceding characters.
    """
    raw = raw_id.strip()
    if len(raw) <= 4:
        return "••••"
    return f"{'•' * (len(raw) - 4)}{raw[-4:]}"


def save_identity_document(file_bytes: bytes, original_filename: str) -> str:
    """
    Save an identity document with an unguessable UUID filename.
    Validates file extension and ensures target directory exists.
    """
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
    ext = Path(original_filename).suffix.lower()
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported document format '{ext}'. Allowed: {', '.join(allowed_extensions)}",
        )

    upload_dir = Path(settings.identity_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    safe_filename = f"{uuid.uuid4().hex}{ext}"
    target_path = upload_dir / safe_filename
    target_path.write_bytes(file_bytes)

    # Return relative storage path with forward slashes
    return f"{settings.identity_dir}/{safe_filename}".replace("\\", "/")


async def submit_identity(
    db: AsyncSession,
    user_id: int,
    id_type: str,
    raw_id_number: str,
    organization_id: Optional[int] = None,
    document_bytes: Optional[bytes] = None,
    document_filename: Optional[str] = None,
) -> UserIdentity:
    """
    Submit a customer identity document for verification.
    Encrypts the ID number and stores the masked representation.
    """
    cleaned_id = raw_id_number.strip()
    if not cleaned_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ID number is required",
        )

    enc_id = encrypt_id_number(cleaned_id)
    masked_id = mask_id_number(cleaned_id)

    doc_path = None
    if document_bytes and document_filename:
        doc_path = save_identity_document(document_bytes, document_filename)

    identity = UserIdentity(
        user_id=user_id,
        organization_id=organization_id,
        id_type=id_type,
        encrypted_id_number=enc_id,
        masked_id_number=masked_id,
        document_path=doc_path,
        status="pending",
    )
    db.add(identity)
    await db.commit()
    await db.refresh(identity)
    return identity


async def check_user_identity_verified(
    db: AsyncSession,
    user_id: int,
    organization_id: Optional[int] = None,
) -> bool:
    """
    Check if the user has an approved, non-expired identity verification record.
    Accepts either an organization-specific approval or a global approval.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserIdentity)
        .where(
            UserIdentity.user_id == user_id,
            UserIdentity.status == "approved",
            or_(
                UserIdentity.organization_id == organization_id,
                UserIdentity.organization_id.is_(None),
            ),
            or_(
                UserIdentity.expires_at.is_(None),
                UserIdentity.expires_at > now,
            ),
        )
    )
    result = await db.execute(stmt)
    return result.scalars().first() is not None


async def review_identity(
    db: AsyncSession,
    identity_id: int,
    reviewer_id: int,
    action: str,  # "approve" | "reject"
    rejection_reason: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> UserIdentity:
    """Approve or reject a pending identity verification document."""
    identity = await db.get(UserIdentity, identity_id)
    if not identity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Identity record not found",
        )

    if action not in ("approve", "reject"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Action must be 'approve' or 'reject'",
        )

    now = datetime.now(timezone.utc)
    identity.reviewed_by_id = reviewer_id
    identity.reviewed_at = now

    if action == "approve":
        identity.status = "approved"
        identity.rejection_reason = None
        identity.expires_at = expires_at
    else:
        identity.status = "rejected"
        identity.rejection_reason = rejection_reason or "Document does not meet verification requirements."

    await db.commit()
    await db.refresh(identity)
    return identity


async def list_user_identities(
    db: AsyncSession,
    user_id: int,
) -> list[UserIdentity]:
    """Retrieve all identity verification submissions for a customer (masked numbers only)."""
    stmt = (
        select(UserIdentity)
        .where(UserIdentity.user_id == user_id)
        .order_by(desc(UserIdentity.created_at))
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_identities_for_review(
    db: AsyncSession,
    organization_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[UserIdentity]:
    """List customer identities for the staff verification desk."""
    stmt = select(UserIdentity).options(selectinload(UserIdentity.user))
    if organization_id is not None:
        stmt = stmt.where(
            or_(
                UserIdentity.organization_id == organization_id,
                UserIdentity.organization_id.is_(None),
            )
        )
    if status_filter:
        stmt = stmt.where(UserIdentity.status == status_filter)

    stmt = stmt.order_by(desc(UserIdentity.created_at)).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())
