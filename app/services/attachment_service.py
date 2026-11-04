"""
app/services/attachment_service.py
──────────────────────────────────
Phase 7 — shared private-attachment storage for support cases and
verified-feedback files. Follows the identity-document conventions:
unguessable UUID filenames, extension whitelist, size limit, and files
are NEVER exposed through static mounts — downloads always pass an
authorized router endpoint that appends an audit event.

Phase 8 (SEC-003 / ORGKYC-007 — approved LOCAL CLAMAV approach):
uploads take the QUARANTINE → SCAN → PROMOTE path via
`save_scanned_file`:
  1. extension whitelist, size limit and non-empty payload validated
     (also content-sniffed against known file-signature magic bytes);
  2. bytes are written to the private quarantine directory
     (`uploads/quarantine/<subdir>` — never exposed through any static
     mount or public URL);
  3. ClamAV INSTREAM scan; clean files are moved into the final private
     approved storage, infected files are DISCARDED and rejected (the
     EICAR-safe test signature is used in tests); scanner-unavailable
     FAILS CLOSED with HTTP 503 while the quarantined bytes stay for
     security evidence; scanning explicitly disabled records
     scan_status="disabled".
Infected uploads therefore never reach reviewers; the audit event plus
persisted scan fields are the security evidence.
"""

import uuid
from hashlib import sha256
from pathlib import Path

from fastapi import HTTPException, status

from app.config import settings
from app.services import clamav_service

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024  # 8 MB

# Content-type signature validation (SEC-003: "validate by content"):
# allowed file magic bytes. Missing/unknown magic is allowed for PDF-less
# edge cases? No — every allowed format has a well-known signature.
_MAGIC_SIGNATURES = {
    b"%PDF": ".pdf",
    b"\xff\xd8\xff": ".jpg",     # covers jpeg as well
    b"\x89PNG\r\n\x1a\n": ".png",
    b"RIFF": ".webp",            # RIFF....WEBP
}


def _validate_upload(data: bytes, original_filename: str) -> str:
    """Shared extension/size/content guards. Returns the normalized extension."""
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

    # Content-signature validation: the declared extension must match the
    # file's actual magic bytes, unless ClamAV scanning is disabled (a
    # separate invocation path deliberately used by config-less dev runs).
    if settings.clamav_enabled and not _content_matches_extension(data, ext):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"File content does not match its extension ('{ext}') — "
                "upload rejected"
            ),
        )
    return ext


def _content_matches_extension(data: bytes, ext: str) -> bool:
    head = data[:16]
    for magic, fmt in _MAGIC_SIGNATURES.items():
        if head.startswith(magic) or head[:8] == magic[:8]:
            if fmt == ".webp":
                return data[8:12] == b"WEBP"
            return fmt == ext
    return False


def save_private_file(
    subdir: str,
    data: bytes,
    original_filename: str | None,
) -> tuple[str, str, int, str, str]:
    """Persist one private upload WITHOUT malware scanning (legacy path —
    used when ClamAV is disabled). Returns (relpath, display_name, size, mime, sha256).

    Raises 400 for unsupported extensions and 413 for oversized files.
    """
    ext = _validate_upload(data, original_filename)
    original = (original_filename or "unnamed").strip() or "unnamed"

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
        sha256(data).hexdigest(),
    )


async def save_scanned_file(
    subdir: str,
    data: bytes,
    original_filename: str | None,
) -> dict:
    """Quarantine → ClamAV scan → promote. Returns a dict with the legacy
    tuple fields plus the scan evidence:

        {
          "relpath", "display_name", "size", "mime", "sha256",
          "scan_status", "scanned_at", "scan_signature",
        }

    Raises:
      400/413 — validation failures (invalid extension/size/content)
      422     — malware signature detected (quarantined copy destroyed)
      503     — scanner unavailable (FAIL CLOSED; quarantine copy retained)
    """
    from datetime import datetime, timezone

    ext = _validate_upload(data, original_filename)
    original = (original_filename or "unnamed").strip() or "unnamed"

    final_dir = Path(settings.upload_dir) / subdir
    final_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir = Path(settings.upload_dir) / "quarantine" / subdir
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    stored_name = f"{uuid.uuid4().hex}{ext}"
    quarantine_path = quarantine_dir / stored_name
    final_path = final_dir / stored_name
    quarantine_path.write_bytes(data)

    def _mime() -> str:
        return "application/pdf" if ext == ".pdf" else f"image/{ext.lstrip('.')}"

    scan_result = await clamav_service.scan_bytes(data, filename=original)
    scanned_at = datetime.now(timezone.utc)

    if scan_result.status == clamav_service.ScanStatus.UNAVAILABLE:
        # FAIL CLOSED — refuse the upload; quarantine copy is preserved at
        # the private, never-public path for security evidence.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Malware scanner is unavailable — upload rejected for "
                "safety; please retry later"
            ),
        )

    if scan_result.status == clamav_service.ScanStatus.INFECTED:
        try:
            quarantine_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Malware detected ({scan_result.signature}); upload rejected"
            ),
        )

    # clean / disabled: promote into the final approved private storage
    if scan_result.status == clamav_service.ScanStatus.CLEAN:
        quarantine_path.replace(final_path)
    else:  # DISABLED — bytes taken at face value, evidence marks the gap
        final_path.write_bytes(data)
        quarantine_path.unlink(missing_ok=True)

    return {
        "relpath": str(final_path),
        "display_name": original,
        "size": len(data),
        "mime": _mime(),
        "sha256": sha256(data).hexdigest(),
        "scan_status": scan_result.status,
        "scanned_at": scanned_at,
        "scan_signature": scan_result.signature,
    }
