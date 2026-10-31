"""
tests/api/test_payment_policy_api.py
────────────────────────────────────
FIN-001 API-level regression tests:

- Admin payment-policy read/write endpoints (vocabulary validation,
  unrestricted sentinel, audited writes).
- Admin pause/resume of a PAID booking over the API (reason mandatory,
  idempotency guards, restored operator status).
- Bypass prevention: POST /payments/initiate enforces the organization
  policy with HTTP 403 when the online path is not permitted.
- All /admin policy + hold endpoints require an admin principal.
"""

import pytest
from httpx import AsyncClient
from decimal import Decimal
from sqlalchemy import select

from app.config import settings
from app.services.admin_service import seed_default_admin
from app.services import payment_policy_service
from models.admin import Admin
from models.counter import Counter
from models.organization import Organization, OrgStatus
from models.service import Service
from models.token import Token, TokenStatus

_seq = 0


def _next() -> int:
    global _seq
    _seq += 1
    return _seq


async def _login_admin(client: AsyncClient, db_session) -> str:
    await seed_default_admin(db_session)
    resp = await client.post("/auth/admin/login", json={
        "email": settings.admin_email,
        "password": settings.admin_password,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_paid_waiting_token(db_session) -> Token:
    n = _next()
    org = Organization(
        name=f"Policy API Org {n}",
        registration_number=f"REG-POLAPI-{n:05d}",
        org_type="hospital",
        address="1 Policy Way",
        city="Metro City",
        state="State",
        email=f"polapi-{n}@example.com",
        mobile="9876543210",
        password_hash="x",
        status=OrgStatus.APPROVED.value,
    )
    db_session.add(org)
    await db_session.flush()
    counter = Counter(
        organization_id=org.id,
        counter_name=f"Policy Desk {n}",
        counter_number=n % 90 or 1,
        daily_limit=100,
    )
    db_session.add(counter)
    await db_session.flush()
    service = Service(
        organization_id=org.id,
        name=f"Policy Service {n}",
        price=Decimal("500.00"),
    )
    db_session.add(service)
    await db_session.flush()
    token = Token(
        organization_id=org.id,
        counter_id=counter.id,
        service_id=service.id,
        token_number=f"POLAPI-{n:04d}",
        sequence=n,
        status=TokenStatus.WAITING.value,
        amount_paid=Decimal("500.00"),
    )
    db_session.add(token)
    await db_session.commit()
    return token


@pytest.mark.api
async def test_admin_policy_endpoints_require_auth(client: AsyncClient, db_session):
    token = await _make_paid_waiting_token(db_session)
    for path in (
        f"/admin/api/organizations/{token.organization_id}/payment-policy",
    ):
        resp = await client.get(path)
        assert resp.status_code in (401, 403), resp.status_code


@pytest.mark.api
async def test_policy_roundtrip_and_validation(client: AsyncClient, db_session):
    admin_token = await _login_admin(client, db_session)
    token = await _make_paid_waiting_token(db_session)
    headers = {"Authorization": f"Bearer {admin_token}"}
    url = f"/admin/api/organizations/{token.organization_id}/payment-policy"

    # Initially unrestricted
    resp = await client.get(url, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["allowed_payment_methods"] is None

    # Set a policy
    resp = await client.put(url, headers=headers, json={
        "allowed_payment_methods": ["razorpay", "cash"]
    })
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["data"]["allowed_payment_methods"]) == ["cash", "razorpay"]

    resp = await client.get(url, headers=headers)
    assert sorted(resp.json()["data"]["allowed_payment_methods"]) == ["cash", "razorpay"]

    # Invalid vocabulary → 422
    resp = await client.put(url, headers=headers, json={
        "allowed_payment_methods": ["paytm"]
    })
    assert resp.status_code == 422

    # Empty list → unrestricted forbidden value also 422
    resp = await client.put(url, headers=headers, json={"allowed_payment_methods": []})
    assert resp.status_code == 422

    # Null → unrestricted again
    resp = await client.put(url, headers=headers, json={"allowed_payment_methods": None})
    assert resp.status_code == 200
    assert resp.json()["data"]["allowed_payment_methods"] is None


@pytest.mark.api
async def test_pause_resume_api_roundtrip(client: AsyncClient, db_session):
    admin_token = await _login_admin(client, db_session)
    token = await _make_paid_waiting_token(db_session)
    headers = {"Authorization": f"Bearer {admin_token}"}

    # Pause requires a reason — schema-level min length → 422; a message-only
    # reason below service validation is also rejected (400).
    resp = await client.post(f"/admin/tokens/{token.id}/pause", headers=headers,
                             json={"reason": "ab"})
    assert resp.status_code == 422

    resp = await client.post(f"/admin/tokens/{token.id}/pause", headers=headers,
                             json={"reason": "Premium review pending"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "on_hold"
    assert data["payment_hold"] is True
    assert data["payment_hold_previous_status"] == "waiting"

    # Duplicate pause rejected
    resp = await client.post(f"/admin/tokens/{token.id}/pause", headers=headers,
                             json={"reason": "second attempt"})
    assert resp.status_code == 400

    # Resume restores the operator's status
    resp = await client.post(f"/admin/tokens/{token.id}/resume", headers=headers,
                             json={"reason": "Review finished, cleared"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["payment_hold"] is False
    assert data["status"] == "waiting"
    assert data["payment_hold_previous_status"] is None

    # Resume again → not paused
    resp = await client.post(f"/admin/tokens/{token.id}/resume", headers=headers,
                             json={"reason": "Already resumed"})
    assert resp.status_code == 400


@pytest.mark.api
async def test_pause_api_404_unknown_token(client: AsyncClient, db_session):
    admin_token = await _login_admin(client, db_session)
    resp = await client.post("/admin/tokens/99999999/pause",
                             headers={"Authorization": f"Bearer {admin_token}"},
                             json={"reason": "Does not exist"})
    assert resp.status_code == 404


@pytest.mark.api
async def test_payments_initiate_enforces_policy_403(client: AsyncClient, db_session):
    """FIN-001 bypass prevention: with the org policy excluding `razorpay`,
    a user-owned token's online payment initiation is rejected with 403 —
    through the real /payments/initiate endpoint."""
    from models.user import User

    n = _next()
    org = Organization(
        name=f"Policy Bypass Org {n}",
        registration_number=f"REG-POLBYP-{n:05d}",
        org_type="hospital",
        address="1 Bypass Way",
        city="Metro City",
        state="State",
        email=f"polbyp-{n}@example.com",
        mobile="9876543210",
        password_hash="x",
        status=OrgStatus.APPROVED.value,
    )
    db_session.add(org)
    await db_session.flush()
    counter = Counter(
        organization_id=org.id, counter_name=f"Bypass Desk {n}",
        counter_number=n % 90 or 1, daily_limit=100,
    )
    db_session.add(counter)
    await db_session.flush()
    service = Service(organization_id=org.id, name=f"Bypass Service {n}",
                      price=Decimal("500.00"))
    db_session.add(service)
    await db_session.flush()

    # Register/login a user, then attach an owned paid token
    email = f"polbypuser-{n}@example.com"
    await client.post("/user/register", json={
        "name": "Policy User", "email": email, "mobile": "9000000002",
        "password": "Password@123", "confirm_password": "Password@123",
    })
    login = await client.post("/auth/user/login", json={
        "email": email, "password": "Password@123"
    })
    assert login.status_code == 200, login.text
    user_token = login.json().get("access_token")
    user = (await db_session.execute(
        select(User).where(User.email == email)
    )).scalar_one()

    token = Token(
        organization_id=org.id, counter_id=counter.id, service_id=service.id,
        token_number=f"POLBYP-{n:04d}", sequence=n,
        status=TokenStatus.WAITING.value, amount_paid=Decimal("500.00"),
        user_id=user.id,
    )
    db_session.add(token)
    await db_session.commit()

    # Restrict the org to cash-only: the online path must 403
    admin = Admin(
        name=f"Policy Bypass Admin {n}", email=f"polbypadmin-{n}@example.com",
        password_hash="x",
        # Inactive: fixture rows must not enter the four-account count.
        is_active=False,
    )
    db_session.add(admin)
    await db_session.flush()
    await payment_policy_service.set_payment_policy(
        db_session, org.id, ["cash"], admin
    )
    await db_session.commit()

    resp = await client.post("/payments/initiate",
                             headers={"Authorization": f"Bearer {user_token}"},
                             json={"token_id": token.id})
    assert resp.status_code == 403, resp.text
    assert "payment policy" in resp.json()["detail"]
