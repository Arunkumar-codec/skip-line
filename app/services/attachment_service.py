"""
app/services/attachment_service.py
─────────────────────────────────
Phase 7 — shared private-attachment storage for support cases and
verified-feedback files. Follows the identity-document conventions:
unguessable UUID filenames, extension whitelist, size limit, and files
are NEVER exposed through static mounts — downloads always pass an
authorized router endpoint that appends an audit event.
"""

import hashlib
import uuid
from pathlib import Path

from fastapi import HTTPException, status

from app.config import settings

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024  # 8 MB


def save_private_file(
    subdir: str,
    data: bytes,
    original_filename: str | None,
) -> tuple[str, str, int, str, str]:
    """Persist one private upload; returns (relpath, display_name, size, mime, sha256).

    Raises 400 for unsupported extensions and 413 for oversized files.
    """
    original = (original_filename or "unnamed").strip() or "unnamed"
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file format '{ext}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            ),
        )
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds the 8 MB attachment limit",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file"
        )

    upload_dir = Path(settings.upload_dir) / subdir
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    target = upload_dir / stored_name
    target.write_bytes(data)

    mime = "application/pdf" if ext == ".pdf" else f"image/{ext.lstrip('.')}"
    return (
        str(target),
        original,
        len(data),
        mime,
        hashlib.sha256(data).hexdigest(),
    )
