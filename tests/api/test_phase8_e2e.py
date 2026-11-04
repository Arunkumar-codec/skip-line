"""
tests/api/test_phase8_e2e.py
────────────────────────────
Phase 8 end-to-end journeys over the HTTP surface. The five active roles
(platform admin, organization principal, registered customer, kiosk
guest device, assisted desk operator) all appear across these scenarios:

- Admin dual-approval suspension (§4.4): impact evidence (ENF-003),
  suspension blocks customer, kiosk AND operator walk-in bookings
  (ENF-002), org narrow access (ENF-004), appeal + admin decision,
  reinstatement with dual approval, and bookings reopening.
- Customer booking with `Idempotency-Key` retry (DB-001): the same key
  replays the originally issued token; an in-flight OPEN key conflicts
  with 409.
- Kiosk guest repeats the same `Idempotency-Key` (KSK-005 channel
  semantics) — the guest must receive the original token, never a
  duplicate.
- Notification preferences over the API (NTF-004): optional types can be
  disabled; essential types are refused (422).
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import employee_service, org_service
from app.utils.jwt import create_access_token
from app.utils.security import hash_password
from models.admin import Admin
from models.branch import Branch
from models.counter import Counter, CounterStatus
from models.idempotency import IdempotencyKey, IdempotencyState
from models.organization import OrgStatus
from models.service import Service
from models.token import Token
from models.user import User
from tests.phase7_helpers import make_org, make_user

pytestmark = pytest.mark.asyncio


def _admin_header(admin: Admin) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(admin.id, 'admin')}"}


def _org_header(org) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(org.id, 'organization')}"}


def _user_header(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, 'user')}"}


def _employee_header(employee) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(employee.id, 'employee')}"}


async def _make_admin(db: AsyncSession, email: str, password: str) -> Admin:
    admin = Admin(
        name=f"Admin {email}",
        email=email,
        password_hash=hash_password(password),
        is_active=True,
    )
    db.add(admin)
    await db.commit()
    return admin


async def _booking_env(db: AsyncSession):
    """org + ACTIVE counter + active free service ready for bookings."""
    org = await make_org(db)
    counter = Counter(
        organization_id=org.id,
        counter_name=f"E2E-{uuid.uuid4().hex[:8]}",
        counter_number=1,
        token_prefix="E",
        daily_limit=100,
        queue_mode="day_queue",
        status=CounterStatus.ACTIVE,
    )
    db.add(counter)
    service = Service(
        organization_id=org.id,
        name=f"Service-{uuid.uuid4().hex[:8]}",
        estimated_minutes=10,
        price=Decimal("0.00"),
        is_active=True,
        requires_identity_verification=False,
    )
    db.add(service)
    await db.commit()
    counter = (await db.execute(
        select(Counter).where(Counter.organization_id == org.id)
    )).scalars().first()
    service = (await db.execute(
        select(Service).where(Service.organization_id == org.id)
    )).scalars().first()
    return org, counter, service


async def _make_employee(db: AsyncSession, org, code: str):
    branch = Branch(
        organization_id=org.id,
        name=f"Branch {code}",
        branch_code=code,
        address="1 E2E Road",
        city="Metro",
        state="ST",
        pincode="560001",
    )
    db.add(branch)
    await db.commit()
    branch = (await db.execute(
        select(Branch).where(Branch.organization_id == org.id)
    )).scalars().first()
    return await employee_service.create_employee(
        db,
        org.id,
        {
            "name": f"Operator {code}",
            "email": f"op_{code}_{uuid.uuid4().hex[:6]}@example.com",
            "employee_code": code,
            "branch_id": branch.id,
            "password": "SecurePassword@123",
        },
    )


def _suspend_body(admin2: Admin, password: str, reason: str) -> dict:
    return {
        "reason": reason,
        "second_approver_email": admin2.email,
        "second_approver_password": password,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 1. The full enforcement journey (admin + org + customer + kiosk + operator)
# ══════════════════════════════════════════════════════════════════════════════

async def test_full_enforcement_journey_suspend_appeal_reinstate(
    client: AsyncClient, db_session: AsyncSession,
):
    admin1_pw, admin2_pw = "FirstAdmin!42", "SecondAdmin!42"
    admin1 = await _make_admin(db_session, f"a1_{uuid.uuid4().hex[:8]}@example.com", admin1_pw)
    admin2 = await _make_admin(db_session, f"a2_{uuid.uuid4().hex[:8]}@example.com", admin2_pw)
    org, counter, service = await _booking_env(db_session)
    user = await make_user(db_session)
    operator = await _make_employee(db_session, org, f"OP-{uuid.uuid4().hex[:6]}")

    user_headers = _user_header(user)

    async def _customer_book():
        return await client.post(
            "/user/tokens/book",
            headers=user_headers,
            json={
                "organization_id": org.id,
                "counter_id": counter.id,
                "service_id": service.id,
            },
        )

    # ── baseline: all channels can book before enforcement ────────────────
    baseline = await _customer_book()
    assert baseline.status_code == 200, baseline.text

    # ── ENF-003: the impact the suspension WOULD have (evidence first) ─────
    impact = await client.get(
        f"/admin/api/organizations/{org.id}/suspension-impact",
        headers=_admin_header(admin1),
    )
    assert impact.status_code == 200
    assert "impact" in impact.json()["data"]
    assert "classification" in impact.json()["data"]

    # ── refuse without a second approver (§4.4) ────────────────────────────
    missing = await client.post(
        f"/admin/organizations/{org.id}/suspend",
        headers=_admin_header(admin1),
        json={"reason": "verified data fabrication"},
    )
    assert missing.status_code == 403
    assert "second" in missing.json()["detail"].lower()

    # same admin as second approver → 403
    same = await client.post(
        f"/admin/organizations/{org.id}/suspend",
        headers=_admin_header(admin1),
        json={
            "reason": "verified data fabrication",
            "second_approver_email": admin1.email,
            "second_approver_password": admin1_pw,
        },
    )
    assert same.status_code == 403
    assert "different" in same.json()["detail"].lower()

    # wrong credentials → 403
    bad = await client.post(
        f"/admin/organizations/{org.id}/suspend",
        headers=_admin_header(admin1),
        json=_suspend_body(admin2, "wrong-password", "verified data fabrication"),
    )
    assert bad.status_code == 403
    assert bad.json()["detail"] == "Second approver authentication failed"

    # ── verified dual approval suspends the organization ───────────────────
    suspend = await client.post(
        f"/admin/organizations/{org.id}/suspend",
        headers=_admin_header(admin1),
        json=_suspend_body(admin2, admin2_pw, "verified data fabrication"),
    )
    assert suspend.status_code == 200, suspend.text
    action_id = int(suspend.json()["message"].split("action #")[-1].rstrip(")"))

    # customer booking is now refused (ENF-002, honest 403 not a 500)
    blocked = await _customer_book()
    assert blocked.status_code == 403
    assert "enforcement" in blocked.json()["detail"].lower()

    # kiosk channel is equally gated
    kiosk_blocked = await client.post(
        "/public/kiosk/book",
        json={
            "organization_id": org.id,
            "counter_id": counter.id,
            "service_id": service.id,
            "guest_name": "Kiosk Visitor",
            "guest_mobile": "9111111111",
        },
    )
    assert kiosk_blocked.status_code == 403

    # operator walk-in channel is also gated (SHIFT/PHY channel)
    walkin_blocked = await client.post(
        "/employee/tokens/book-walkin",
        headers=_employee_header(operator),
        json={
            "counter_id": counter.id,
            "service_id": service.id,
            "customer_name": "Walk-in Guest",
            "customer_phone": "9822334455",
        },
    )
    assert walkin_blocked.status_code == 403

    # ── ENF-004: suspended org keeps ONLY the narrow review surface ────────
    listing = await client.get("/org/api/enforcements", headers=_org_header(org))
    assert listing.status_code == 200
    actions = listing.json()["data"]["enforcements"]
    assert [a["id"] for a in actions] == [action_id]
    assert "data fabrication" in actions[0]["reason"]

    appeal = await client.post(
        f"/org/api/enforcements/{action_id}/appeal",
        headers=_org_header(org),
        json={"reason": "The reported data was corrected on our side; "
                        "please review our evidence pack."},
    )
    assert appeal.status_code == 200, appeal.text
    appeal_id = appeal.json()["data"]["appeal_id"]

    duplicate = await client.post(
        f"/org/api/enforcements/{action_id}/appeal",
        headers=_org_header(org),
        json={"reason": "trying a second appeal"},
    )
    assert duplicate.status_code == 409

    # everything else on the org panel is refused while suspended
    denied = await client.get("/org/api/kyc", headers=_org_header(org))
    assert denied.status_code == 403
    assert "enforcement" in denied.json()["detail"].lower()

    # ── the admin decides the appeal (may request re-verification) ─────────
    decide = await client.post(
        f"/admin/api/enforcement-appeals/{appeal_id}/decide",
        headers=_admin_header(admin2),
        json={
            "status": "approved",
            "decision_reason": "evidence pack verified",
            "re_verification_requested": True,
        },
    )
    assert decide.status_code == 200, decide.text

    # ── reinstatement also needs dual approval (§4.4) ──────────────────────
    reinstate_missing = await client.post(
        f"/admin/organizations/{org.id}/reinstate",
        headers=_admin_header(admin1),
        json={"reason": "org corrected everything"},
    )
    assert reinstate_missing.status_code == 403

    reinstate = await client.post(
        f"/admin/organizations/{org.id}/reinstate",
        headers=_admin_header(admin1),
        json={
            "reason": "org corrected the data; verified",
            "second_approver_email": admin2.email,
            "second_approver_password": admin2_pw,
        },
    )
    assert reinstate.status_code == 200, reinstate.text
    assert reinstate.json()["data"]["status"] == OrgStatus.APPROVED.value

    # bookings reopen across the customer channel
    reopened = await _customer_book()
    assert reopened.status_code == 200, reopened.text


# ══════════════════════════════════════════════════════════════════════════════
# 2. Customer booking idempotency (DB-001)
# ══════════════════════════════════════════════════════════════════════════════

async def test_customer_booking_idempotency_replay(
    client: AsyncClient, db_session: AsyncSession,
):
    org, counter, service = await _booking_env(db_session)
    user = await make_user(db_session)
    user_headers = _user_header(user)
    key = f"book-{uuid.uuid4().hex}"

    body = {
        "organization_id": org.id,
        "counter_id": counter.id,
        "service_id": service.id,
    }

    first = await client.post(
        "/user/tokens/book",
        headers=dict(user_headers, **{"Idempotency-Key": key}),
        json=body,
    )
    assert first.status_code == 200, first.text
    token_1 = first.json()["data"]["id"]

    # network retry replays the ORIGINAL token instead of double-booking
    second = await client.post(
        "/user/tokens/book",
        headers=dict(user_headers, **{"Idempotency-Key": key}),
        json=body,
    )
    assert second.status_code == 200
    assert second.json()["data"]["id"] == token_1

    # exactly one token sits in the queue for this organization
    rows = (await db_session.execute(
        select(Token).where(Token.organization_id == org.id)
    )).scalars().all()
    assert len(rows) == 1

    # a completed key is durable: the row is now COMPLETED
    idem = (await db_session.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.organization_id == org.id,
            IdempotencyKey.key == key,
        )
    )).scalar_one()
    assert idem.status == IdempotencyState.COMPLETED


async def test_customer_booking_conflicts_on_open_key(
    client: AsyncClient, db_session: AsyncSession,
):
    """A key currently open in another in-flight request must conflict
    with 409 — a concurrent duplicate booking never proceeds silently."""
    org, counter, service = await _booking_env(db_session)
    user = await make_user(db_session)
    user_headers = _user_header(user)
    key = f"open-{uuid.uuid4().hex}"

    # a concurrent attempt owns the key: durable OPEN row
    db_session.add(IdempotencyKey(
        organization_id=org.id,
        key=key,
        operation="token.create",
        status=IdempotencyState.OPEN,
    ))
    await db_session.commit()

    conflict = await client.post(
        "/user/tokens/book",
        headers=dict(user_headers, **{"Idempotency-Key": key}),
        json={
            "organization_id": org.id,
            "counter_id": counter.id,
            "service_id": service.id,
        },
    )
    assert conflict.status_code == 409
    assert "idempotency conflict" in conflict.json()["detail"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Kiosk guest repeat with the same Idempotency-Key (KSK-005 channel)
# ══════════════════════════════════════════════════════════════════════════════

async def test_kiosk_repeat_same_idempotency_key_never_duplicates(
    client: AsyncClient, db_session: AsyncSession,
):
    org, counter, service = await _booking_env(db_session)
    key = f"kiosk-{uuid.uuid4().hex}"
    body = {
        "organization_id": org.id,
        "counter_id": counter.id,
        "service_id": service.id,
        "guest_name": "Kiosk Visitor",
        "guest_mobile": "9111111111",
    }

    first = await client.post(
        "/public/kiosk/book",
        headers={"Idempotency-Key": key},
        json=body,
    )
    assert first.status_code == 200, first.text
    token_1 = first.json()["data"]["id"]

    # printer retry: SAME key must return the SAME token
    retry = await client.post(
        "/public/kiosk/book",
        headers={"Idempotency-Key": key},
        json=body,
    )
    assert retry.status_code == 200
    assert retry.json()["data"]["id"] == token_1

    tokens = (await db_session.execute(
        select(Token).where(Token.organization_id == org.id)
    )).scalars().all()
    assert len(tokens) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. Notification preferences (NTF-004) over the API
# ══════════════════════════════════════════════════════════════════════════════

async def test_notification_preferences_api_roundtrip(
    client: AsyncClient, db_session: AsyncSession,
):
    user = await make_user(db_session)
    headers = _user_header(user)

    default = await client.get("/notifications/preferences", headers=headers)
    assert default.status_code == 200
    data = default.json()["data"]
    assert data["disabled_types"] == []
    assert "security_notice" in data["essential_types"]

    # disable an OPTIONAL type (body: the typed list)
    updated = await client.put(
        "/notifications/preferences",
        headers=headers,
        json=["token_created"],
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["disabled_types"] == ["token_created"]

    fetched = await client.get("/notifications/preferences", headers=headers)
    assert fetched.json()["data"]["disabled_types"] == ["token_created"]

    # ESSENTIAL types can never be disabled — 422, preferences unchanged
    refused = await client.put(
        "/notifications/preferences",
        headers=headers,
        json=["security_notice"],
    )
    assert refused.status_code == 422
    assert "essential" in refused.json()["detail"].lower()

    still = await client.get("/notifications/preferences", headers=headers)
    assert still.json()["data"]["disabled_types"] == ["token_created"]
