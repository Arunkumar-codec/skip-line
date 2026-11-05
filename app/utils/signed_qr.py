"""
app/utils/signed_qr.py
──────────────────────
Signed opaque token references (QR-002, Release Gate 6).

A token reference ("ref") is a self-authenticating opaque string:

    {payload_b64url}.{sig_b64url}

where payload is compact JSON (base64url, unpadded):

    t  internal token id          (required for lookup; never guessable)
    o  organization id            (tenant scope binding)
    p  purpose ("scan" | "track") (purpose-bound key + payload tag)
    i  issued-at epoch seconds
    e  expiry epoch seconds
    n  random nonce (makes the ref unguessable and unique per issue)
    v  payload format version

The signature is HMAC-SHA256 over the exact payload bytes, keyed with a
purpose-scoped subkey derived from `settings.secret_key` (never stored or
logged). Verification uses `hmac.compare_digest` (constant time) so tampered
or forged refs fail safely, and expiry is checked against the signed `e`
field. Plaintext refs leak nothing and cannot be enumerated: two tokens with
identical scopes still produce completely different refs because of the
random nonce (and the signature itself is unpredictable).

TTL values are PROVISIONAL (docs/OPEN_DECISIONS.md Decision 19): the
requirements do not specify scan/track lifetimes, so safe defaults are used
and exposed as module constants.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from app.config import settings

# ── Protocol constants ────────────────────────────────────────────────────────

REF_VERSION = 1

PURPOSE_SCAN = "scan"       # authorized employee check-in QR (QR-003)
PURPOSE_TRACK = "track"     # public live-tracking QR (QR-002)
PURPOSE_RECEIPT = "receipt" # guest receipt/QR download authorization (QR-001 hardening)

# Decision 19 (PROVISIONAL — recorded in docs/OPEN_DECISIONS.md):
# requirements do not define these lifetimes.
SCAN_REF_TTL_SECONDS: int = 24 * 60 * 60            # 24 hours
TRACK_REF_TTL_SECONDS: int = 90 * 24 * 60 * 60      # 90 days
RECEIPT_REF_TTL_SECONDS: int = 24 * 60 * 60         # 24 hours (also provisional)

# Verification outcomes (machine-readable; Tampered/Invalid are distinct so
# the audit trail can tell deliberate forgery attempts from garbage input).
STATUS_VALID = "VALID"
STATUS_EXPIRED = "EXPIRED"
STATUS_TAMPERED = "TAMPERED"
STATUS_INVALID = "INVALID"

_KEY_CONTEXT = b"skipline-qr-v1"


@dataclass(frozen=True)
class RefPayload:
    """Parsed contents of a successfully verified reference."""

    token_id: int
    org_id: int
    purpose: str
    issued_at: int
    expires_at: int
    nonce: str


def _purposes() -> tuple:
    return (PURPOSE_SCAN, PURPOSE_TRACK, PURPOSE_RECEIPT)


def _derivation_key(purpose: str) -> bytes:
    """Purpose-scoped HMAC subkey derived from the master secret.

    A ref signed for one purpose can never verify as another purpose because
    the subkey differs, and rotating `settings.secret_key` immediately
    invalidates every existing ref (QR-003 revocation support).
    """
    salt = f"{_KEY_CONTEXT.decode()}:{purpose}".encode()
    return hmac.new(settings.secret_key.encode(), salt, hashlib.sha256).digest()


def _b64url_encode(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> Optional[bytes]:
    import base64

    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)
    except Exception:
        return None


def _sign(payload_b64: str, purpose: str) -> str:
    digest = hmac.new(
        _derivation_key(purpose), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64url_encode(digest)


def create_token_ref(
    token_id: int,
    org_id: int,
    purpose: str,
    ttl_seconds: Optional[int] = None,
    now: Optional[float] = None,
) -> str:
    """Create a signed opaque reference for one token and purpose."""
    if purpose not in _purposes():
        raise ValueError(f"Unknown ref purpose: {purpose}")
    if ttl_seconds is None:
        if purpose == PURPOSE_SCAN:
            ttl_seconds = SCAN_REF_TTL_SECONDS
        elif purpose == PURPOSE_TRACK:
            ttl_seconds = TRACK_REF_TTL_SECONDS
        else:
            ttl_seconds = RECEIPT_REF_TTL_SECONDS
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    current = int(now if now is not None else time.time())
    payload = {
        "v": REF_VERSION,
        "t": int(token_id),
        "o": int(org_id),
        "p": purpose,
        "i": current,
        "e": current + int(ttl_seconds),
        # 16 url-safe chars = 96 bits of entropy: refs cannot be guessed or
        # enumerated even when issued simultaneously for sequential tokens.
        "n": secrets.token_urlsafe(12),
    }
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{payload_b64}.{_sign(payload_b64, purpose)}"


def track_ref_url(token_id: int, org_id: int, base_url: str) -> str:
    """Full public tracking URL embedded in QR codes (QR-002)."""
    return f"{base_url}/public/track/{create_token_ref(token_id, org_id, PURPOSE_TRACK)}"


def receipt_ref(token_id: int, org_id: int) -> str:
    """
    Signed receipt-download reference for guest tokens (QR-001 hardening).

    A guest receipt must never be downloadable merely by knowing or guessing
    the numeric token id; possession of this purpose-bound, expiring ref is
    the authorization. Purpose-binding means a track/scan ref can never
    authorize a receipt download and vice versa.
    """
    return create_token_ref(token_id, org_id, PURPOSE_RECEIPT)


def verify_token_ref(
    ref: str,
    purpose: str,
    now: Optional[float] = None,
) -> tuple[str, Optional[RefPayload]]:
    """
    Verify a reference cryptographically.

    Returns (status, payload|None). Status is one of STATUS_VALID,
    STATUS_EXPIRED, STATUS_TAMPERED or STATUS_INVALID. Never raises for
    attacker-supplied input; the outcome must be auditable (QR-004/QR-005).
    """
    if not isinstance(ref, str) or ref.count(".") != 1:
        return STATUS_INVALID, None

    payload_b64, sig_b64 = ref.split(".", 1)
    raw = _b64url_decode(payload_b64)
    if raw is None or not sig_b64 or len(sig_b64) > 172 or len(raw) > 2048:
        return STATUS_INVALID, None

    expected_sig = _sign(payload_b64, purpose)
    supplied_sig = _b64url_decode(sig_b64)
    if supplied_sig is None or not hmac.compare_digest(
        supplied_sig, _b64url_decode(expected_sig) or b""
    ):
        return STATUS_TAMPERED, None

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return STATUS_INVALID, None

    if not isinstance(data, dict):
        return STATUS_INVALID, None
    if data.get("v") != REF_VERSION:
        return STATUS_INVALID, None
    if data.get("p") != purpose:
        # Purpose mismatch on a well-formed payload: the signature above was
        # verified under the caller's purpose key, so a foreign-purpose ref
        # would already have failed signature verification. This guard is
        # defense-in-depth against future key reuse mistakes.
        return STATUS_INVALID, None
    try:
        payload = RefPayload(
            token_id=int(data["t"]),
            org_id=int(data["o"]),
            purpose=purpose,
            issued_at=int(data["i"]),
            expires_at=int(data["e"]),
            nonce=str(data["n"]),
        )
    except (KeyError, TypeError, ValueError):
        return STATUS_INVALID, None

    current = int(now if now is not None else time.time())
    if payload.expires_at <= current:
        return STATUS_EXPIRED, None

    return STATUS_VALID, payload
