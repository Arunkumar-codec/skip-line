"""
app/services/payment_policy_service.py
──────────────────────────────────────
Phase 5 FIN-001: Organization payment-method policy + paid-booking pause.

Implements:
  - Policy storage on `organizations.allowed_payment_methods` (nullable
    JSON array of PaymentMethod enum values; NULL = unrestricted).
  - `ensure_method_allowed` — the single enforcement primitive used by
    EVERY payment-acceptance point (online Razorpay initiate / certify
    counter walk-in), so the policy cannot be bypassed through an
    alternate flow.
  - `set_payment_policy` — admin-only write with audit trail.
  - `pause_paid_booking` / `resume_paid_booking` — admin pause of a PAID
    booking. Pause reuses the existing `TokenStatus.ON_HOLD` queue
    semantics (capacity stays consumed per Open Decision 16: pause is
    NOT a cancellation); operators cannot call/progress/complete a
    token that is on a payment hold (guard lives in
    `token_service.perform_token_action`, so every endpoint that mutates
    queue state is covered).

Design constraints (recorded in OPEN_DECISIONS / FEATURE_REQUIREMENTS_MATRIX):
- Only bookings with `amount_paid > 0` in WAITING/CALLED/IN_PROGRESS are
  pauseable — free bookings and terminal statuses aren't.
- Pause and resume require a reason (min 3 chars) and write audit events.
"""

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import audit_service
from models.organization import Organization
from models.payment import PaymentMethod
from models.token import Token, TokenStatus


class PolicyMethod(str, enum.Enum):
    """Vocabulary accepted inside a payment policy."""

    RAZORPAY = PaymentMethod.RAZORPAY.value
    UPI = PaymentMethod.UPI.value
    CASH = PaymentMethod.CASH.value
    WAIVED = PaymentMethod.WAIVED.value
    FREE = PaymentMethod.FREE.value
    PAY_AT_COUNTER = PaymentMethod.PAY_AT_COUNTER.value


# Walk-in request payment-method → policy vocabulary mapping.
# "online" walk-ins pay later through Razorpay checkout, so they are
# validated against the "razorpay" policy entry (bypass prevention).
WALKIN_METHOD_TO_POLICY: dict[str, str] = {
    "waived": PolicyMethod.WAIVED.value,
    "cash": PolicyMethod.CASH.value,
    "pay_at_counter": PolicyMethod.PAY_AT_COUNTER.value,
    "online": PolicyMethod.RAZORPAY.value,
}

# Operator actions blocked while a payment hold is active.
_PAYMENT_HOLD_BLOCKED_ACTIONS = ("call", "progress", "complete")

# FIN-001 pause eligibility: paid bookings only, active queue statuses.
_PAUSABLE_STATUSES = (
    TokenStatus.WAITING,
    TokenStatus.CALLED,
    TokenStatus.IN_PROGRESS,
)

_MIN_REASON_LENGTH = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_reason(reason: str, what: str) -> str:
    cleaned = (reason or "").strip()
    if len(cleaned) < _MIN_REASON_LENGTH:
        raise ValueError(f"A reason of at least {_MIN_REASON_LENGTH} characters is required to {what}")
    return cleaned


def validate_policy_methods(methods: list[str] | None) -> list[str] | None:
    """Validate the policy vocabulary. Returns the canonical sorted list or
    None (unrestricted)."""
    if methods is None:
        return None
    if not isinstance(methods, list):
        raise ValueError("allowed_payment_methods must be a list of payment method names")
    cleaned: set[str] = set()
    for raw in methods:
        if not isinstance(raw, str):
            raise ValueError("allowed_payment_methods must contain strings only")
        value = raw.strip()
        if not value:
            raise ValueError("allowed_payment_methods contains an empty entry")
        if value not in {m.value for m in PolicyMethod}:
            raise ValueError(
                f"Unknown payment method: {value!r}. Allowed values: "
                f"{sorted(m.value for m in PolicyMethod)}"
            )
        cleaned.add(value)
    if not cleaned:
        # An empty list must never be persisted: NULL means "unrestricted",
        # an empty list would mean "no method usable at all" and both the
        # reader and storing semantics would disagree. Explicit is required.
        raise ValueError(
            "allowed_payment_methods cannot be empty — pass null to lift "
            "all restrictions"
        )
    return sorted(cleaned)


def method_allowed(
    allowed: Optional[list[str] | list] | None, method: str
) -> bool:
    """Pure check — `allowed` is the stored (possibly legacy) list and
    `method` a PaymentMethod vocabulary value."""
    if not allowed:  # NULL / empty handled at write time (None only persisted)
        # A stored empty list must not suddenly allow everything; but the
        # writer never persists empty lists, so treat NULL as unrestricted.
        return True
    return method in allowed


async def get_payment_policy(
    db: AsyncSession, organization_id: int
) -> Optional[list[str]]:
    """Return the org's policy list, or None when unrestricted."""
    organization = await db.get(Organization, organization_id)
    if organization is None:
        raise ValueError("Organization not found")
    return list(organization.allowed_payment_methods) if organization.allowed_payment_methods else None


async def ensure_method_allowed(
    db: AsyncSession, organization_id: int, method: str
) -> None:
    """Raise PermissionError when the organization's configured policy does
    not permit `method`. Every payment-acceptance point calls this BEFORE
    any payment or token row is created."""
    organization = await db.get(Organization, organization_id)
    if organization is None:
        raise ValueError("Organization not found")
    allowed = organization.allowed_payment_methods
    if allowed is not None and not method_allowed(allowed, method):
        raise PermissionError(
            "Payment method not permitted by this organization's payment policy"
        )


async def set_payment_policy(
    db: AsyncSession,
    organization_id: int,
    methods: Optional[list[str]],
    admin: "Admin",
) -> Organization:
    """Admin-only write (FIN-001). Validates vocabulary, persists the policy
    (None = unrestricted), and writes an audit event."""
    organization = await db.get(Organization, organization_id)
    if organization is None:
        raise ValueError("Organization not found")
    canonical = validate_policy_methods(methods)
    previous = list(organization.allowed_payment_methods) if organization.allowed_payment_methods else None
    organization.allowed_payment_methods = canonical
    await db.commit()
    await db.refresh(organization)

    await audit_service.log_audit_event(
        db,
        action="payment.policy.updated",
        actor_type="admin",
        actor_id=admin.id,
        actor_email=admin.email,
        organization_id=organization.id,
        target_type="organization",
        target_id=str(organization.id),
        details={
            "allowed_payment_methods": canonical,
            "previous_allowed_payment_methods": previous,
        },
    )
    return organization


async def pause_paid_booking(
    db: AsyncSession,
    token: Token,
    admin: "Admin",
    reason: str,
) -> Token:
    """Admin-paid booking pause (FIN-001).

    Guards: amount_paid > 0, status in WAITING/CALLED/IN_PROGRESS, and no
    active hold already. Authorization is enforced at the router level
    (`get_current_admin` — platform finance admins administer any org).
    Keeps capacity consumed (status in active sets everywhere) by reusing
    ON_HOLD; resume restores `payment_hold_previous_status`."""
    if (token.amount_paid or 0) <= 0:
        raise ValueError("Only paid bookings can be paused")
    if token.payment_hold:
        raise ValueError("This booking is already paused")
    if token.status not in _PAUSABLE_STATUSES:
        raise ValueError("Only a booking in waiting/called/in-progress can be paused")
    cleaned = _validate_reason(reason, "pause a paid booking")

    previous = token.status.value if hasattr(token.status, "value") else str(token.status)
    token.payment_hold_previous_status = previous
    token.status = TokenStatus.ON_HOLD
    token.payment_hold = True
    token.payment_hold_reason = cleaned
    token.payment_hold_at = _utcnow()
    token.payment_hold_by = admin.id
    await db.commit()
    await db.refresh(token)

    await audit_service.log_audit_event(
        db,
        action="token.payment_hold.paused",
        actor_type="admin",
        actor_id=admin.id,
        actor_email=admin.email,
        organization_id=token.organization_id,
        target_type="token",
        target_id=str(token.id),
        details={
            "reason": cleaned,
            "previous_status": token.payment_hold_previous_status,
            "token_number": token.token_number,
            "amount_paid": str(token.amount_paid),
        },
    )
    return token


async def resume_paid_booking(
    db: AsyncSession,
    token: Token,
    admin: "Admin",
    reason: str,
) -> Token:
    """Reverse of `pause_paid_booking`: restores the operator's previous
    queue status and clears the hold flags. Authorization is enforced at
    the router level (`get_current_admin`)."""
    if not token.payment_hold:
        raise ValueError("This booking is not paused")
    cleaned = _validate_reason(reason, "resume a paid booking")

    previous = token.payment_hold_previous_status or TokenStatus.WAITING.value
    token.status = TokenStatus(previous)
    token.payment_hold = False
    token.payment_hold_previous_status = None
    token.payment_hold_at = None
    token.payment_hold_by = None
    # Keep the original pause reason for audit continuity; the audit event
    # below records the resume reason.
    await db.commit()
    await db.refresh(token)

    await audit_service.log_audit_event(
        db,
        action="token.payment_hold.resumed",
        actor_type="admin",
        actor_id=admin.id,
        actor_email=admin.email,
        organization_id=token.organization_id,
        target_type="token",
        target_id=str(token.id),
        details={"reason": cleaned, "restored_status": previous, "token_number": token.token_number},
    )
    return token
