"""
app/services/scan_service.py
────────────────────────────
Authorized scanner check-in (QR-003/QR-004/QR-005).

An authenticated, active-shift employee scans a token QR. The QR carries a
HMAC-signed opaque reference (app/utils/signed_qr.py). This service:

  1. verifies the signature, purpose and expiry of the reference,
  2. resolves the token with full tenant isolation,
  3. applies the QR-004 result decision table,
  4. marks the first successful check-in idempotently (QR-003), and
  5. records one audit row per attempt (QR-005) — inside the caller's
     transaction; the caller owns commit/rollback.

Checks are ordered so the cheapest and least revealing checks run first and
forged references never touch the database.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.utils import signed_qr
from models.counter import Counter
from models.counter_shift import CounterShift, CounterShiftStatus
from models.token import Token, TokenStatus
from models.token_scan import ScanResult, TokenScan


class ScanOutcome:
    """Result of one scanning attempt."""

    def __init__(self, result: ScanResult, token: Optional[Token], message: str):
        self.result = result
        self.token = token
        self.message = message

    @property
    def is_valid(self) -> bool:
        return self.result == ScanResult.VALID


# Status → QR-004 result mapping for tokens that cannot be checked in.
_STATUS_RESULT = {
    TokenStatus.COMPLETED: ScanResult.COMPLETED,
    TokenStatus.CANCELLED: ScanResult.CANCELLED,
    TokenStatus.SKIPPED: ScanResult.SKIPPED,
    # Called / in progress tokens were implicitly presented earlier; mapping
    # them to ALREADY_CHECKED_IN keeps scanning idempotent and non-fatal.
    TokenStatus.CALLED: ScanResult.ALREADY_CHECKED_IN,
    TokenStatus.IN_PROGRESS: ScanResult.ALREADY_CHECKED_IN,
    TokenStatus.ON_HOLD: ScanResult.ALREADY_CHECKED_IN,
}


async def get_active_shift_for_employee(
    db: AsyncSession, organization_id: int, employee_id: int
) -> Optional[CounterShift]:
    """ACTIVE approved shift for any counter of this employee, today."""
    from sqlalchemy import select, and_

    today = datetime.now(timezone.utc).date()
    result = await db.execute(
        select(CounterShift)
        .where(
            and_(
                CounterShift.organization_id == organization_id,
                CounterShift.employee_id == employee_id,
                CounterShift.shift_date == today,
                CounterShift.status == CounterShiftStatus.ACTIVE,
            )
        )
        .order_by(CounterShift.id.asc())
    )
    return result.scalars().first()


async def scan_token(
    db: AsyncSession,
    employee,                      # models.employee.Employee
    ref: str,
    counter_id: Optional[int] = None,
    device_id: Optional[str] = None,
) -> ScanOutcome:
    """
    Process one scan attempt and return the outcome. The DB changes
    (check-in marking + audit row) stay uncommitted — callers own the
    transaction boundary so scans can participate in larger requests.

    Wrong-tenant, tampered and invalid refs are audited against a synthetic
    organization row only when the ref itself declares the organization
    (its signature is invalid, so we never trust it — such rows are audited
    under the scanner's own organization and reveal nothing).
    """
    organization_id = employee.organization_id

    # 1. Cryptographic verification first (QR-002/QR-003) — never queries
    #    the database for a signature that fails.
    status, payload = signed_qr.verify_token_ref(ref, signed_qr.PURPOSE_SCAN)
    if status != signed_qr.STATUS_VALID:
        result = {
            signed_qr.STATUS_EXPIRED: ScanResult.EXPIRED,
            signed_qr.STATUS_TAMPERED: ScanResult.TAMPERED,
            signed_qr.STATUS_INVALID: ScanResult.INVALID,
        }[status]
        await _record(
            db, organization_id, None, employee.id, counter_id, result, device_id
        )
        logger.info("Scan rejected: {} employee={} org={}", result, employee.id, organization_id)
        return ScanOutcome(result, None, _MESSAGE[result])

    token_id, ref_org_id = payload.token_id, payload.org_id

    # 2. Tenant isolation (QR-003: "validates ... organization"). A ref
    #    signed for another organization reveals nothing here.
    if ref_org_id != organization_id:
        await _record(
            db, organization_id, None, employee.id, counter_id,
            ScanResult.WRONG_ORG, device_id,
        )
        # Phase 8 (ENF-002): repeated wrong-tenant attempts feed the
        # suspicious-cross-tenant-reference risk rule (inside the caller's
        # transaction; never aborts the scan response).
        try:
            from app.services import financial_risk_service as frs
            await frs.evaluate_cross_tenant_reference_risk(
                db, organization_id, commit=False
            )
        except Exception:  # pragma: no cover — advisory rule
            logger.exception("Cross-tenant risk evaluation failed")
        return ScanOutcome(ScanResult.WRONG_ORG, None, _MESSAGE[ScanResult.WRONG_ORG])

    token = await db.get(Token, token_id)
    if not token or token.organization_id != organization_id:
        # A signed ref whose token row no longer exists or moved tenant.
        await _record(
            db, organization_id, None, employee.id, counter_id,
            ScanResult.INVALID, device_id,
        )
        return ScanOutcome(ScanResult.INVALID, None, _MESSAGE[ScanResult.INVALID])

    # 3. Branch scoping (QR-003: "validates ... branch"): if both the scan
    #    desk and the token's counter belong to branches, they must match.
    scan_counter: Optional[Counter] = None
    token_counter = await db.get(Counter, token.counter_id)
    if counter_id:
        scan_counter = await db.get(Counter, counter_id)
        if (
            scan_counter
            and scan_counter.organization_id == organization_id
            and scan_counter.branch_id is not None
            and token_counter is not None
            and token_counter.branch_id is not None
            and scan_counter.branch_id != token_counter.branch_id
        ):
            await _record(
                db, organization_id, token.id, employee.id, counter_id,
                ScanResult.WRONG_BRANCH, device_id,
            )
            return ScanOutcome(
                ScanResult.WRONG_BRANCH, token, _MESSAGE[ScanResult.WRONG_BRANCH]
            )

    # 4. Token state / duplicate-scan decision table (QR-004) — idempotent:
    #    repeated scans keep returning the same verdict and re-write nothing.
    now = datetime.now(timezone.utc)
    if token.checked_in_at:
        result = _STATUS_RESULT.get(token.status, ScanResult.ALREADY_CHECKED_IN)
        if token.status == TokenStatus.WAITING:
            result = ScanResult.ALREADY_CHECKED_IN
        await _record(
            db, organization_id, token.id, employee.id, counter_id,
            result, device_id,
        )
        return ScanOutcome(result, token, _MESSAGE[result])

    if token.status == TokenStatus.WAITING:
        token.checked_in_at = now
        token.checked_in_by = employee.id
        await _record(
            db, organization_id, token.id, employee.id, counter_id,
            ScanResult.VALID, device_id,
        )
        logger.info(
            "Token {} checked in by employee {} (org {})",
            token.token_number, employee.id, organization_id,
        )
        return ScanOutcome(ScanResult.VALID, token, _MESSAGE[ScanResult.VALID])

    result = _STATUS_RESULT.get(token.status, ScanResult.INVALID)
    if token.status in (TokenStatus.CALLED, TokenStatus.IN_PROGRESS, TokenStatus.ON_HOLD):
        # Present at the desk but checked in through a pre-scanner flow:
        # backfill the check-in marker for the audit trail.
        token.checked_in_at = now
        token.checked_in_by = employee.id
    await _record(
        db, organization_id, token.id, employee.id, counter_id, result, device_id
    )
    return ScanOutcome(result, token, _MESSAGE[result])


async def _record(
    db: AsyncSession,
    organization_id: int,
    token_id: Optional[int],
    employee_id: int,
    counter_id: Optional[int],
    result: ScanResult,
    device_id: Optional[str],
) -> None:
    """QR-005 audit row for one attempt."""
    db.add(
        TokenScan(
            organization_id=organization_id,
            token_id=token_id,
            employee_id=employee_id,
            counter_id=counter_id,
            result=result,
            device_id=device_id or None,
        )
    )


# token_scans.token_id is nullable: a scan that fails before token
# resolution (tampered / invalid ref) is audited with token_id NULL while
# still being attributed to the scanner's organization (QR-005).


_MESSAGE = {
    ScanResult.VALID: "Check-in confirmed",
    ScanResult.ALREADY_CHECKED_IN: "Token was already checked in",
    ScanResult.COMPLETED: "Token already completed",
    ScanResult.CANCELLED: "Token already cancelled",
    ScanResult.SKIPPED: "Token already skipped",
    ScanResult.EXPIRED: "QR code expired — issue a new receipt",
    ScanResult.WRONG_ORG: "QR code belongs to a different organization",
    ScanResult.WRONG_BRANCH: "Token belongs to a different branch",
    ScanResult.TAMPERED: "QR code failed integrity verification",
    ScanResult.INVALID: "QR code could not be read as a valid token",
}


def message_for(result: ScanResult) -> str:
    return _MESSAGE[result]


def result_dict(outcome: ScanOutcome, extra: Optional[dict[str, Any]] = None) -> dict:
    """API payload for a scan outcome (QR-004 vocabulary + safe details)."""
    data: dict[str, Any] = {
        "result": outcome.result.value,
        "message": outcome.message,
    }
    if outcome.token is not None:
        data["token_id"] = outcome.token.id
        data["token_number"] = outcome.token.token_number
        data["status"] = (
            outcome.token.status.value
            if hasattr(outcome.token.status, "value")
            else outcome.token.status
        )
        data["checked_in_at"] = (
            outcome.token.checked_in_at.isoformat() if outcome.token.checked_in_at else None
        )
    if extra:
        data.update(extra)
    return data
