"""
tests/integration/test_full_flow.py
─────────────────────────────────────
End-to-end integration tests covering:
  1. Admin seeds on startup
  2. Organization registers → admin approves → org logs in
  3. Counter + service created
  4. User registers and books a token
  5. Organization calls → completes the token
  6. User can see token in history
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.admin_service import seed_default_admin
from app.config import settings


@pytest.mark.integration
async def test_admin_seed_creates_admin(db_session: AsyncSession):
    """Seeding should create exactly one admin if none exists."""
    await seed_default_admin()
    from sqlalchemy import select, func
    from models.admin import Admin
    count = (await db_session.execute(select(func.count()).select_from(Admin))).scalar()
    assert count >= 1


@pytest.mark.integration
async def test_full_token_lifecycle(client: AsyncClient, db_session: AsyncSession):
    """
    Full lifecycle:
      register org → admin approves → create counter/service →
      user registers → user books token → org calls → org completes →
      user sees token in history
    """
    # ── 1. Seed admin ──────────────────────────────────────────────────────
    await seed_default_admin()

    # ── 2. Register organization ───────────────────────────────────────────
    org_resp = await client.post("/org/register", json={
        "name": "Integration Test Hospital",
        "registration_number": "ITH-INT-001",
        "org_type": "hospital",
        "address": "1 Test Road",
        "city": "Testville",
        "state": "Testland",
        "email": "ith@integration.test",
        "mobile": "9000000000",
        "password": "OrgPass@123",
        "confirm_password": "OrgPass@123",
    })
    assert org_resp.status_code == 200, org_resp.text
    org_id = org_resp.json()["data"]["id"]

    # ── 3. Admin approves the org ──────────────────────────────────────────
    # Login as admin
    admin_login = await client.post("/auth/admin/login", json={
        "email": settings.admin_email,
        "password": settings.admin_password,
    })
    assert admin_login.status_code == 200, f"Admin login failed: {admin_login.text}"
    admin_token = admin_login.json()["access_token"]

    approve = await client.post(
        f"/admin/organizations/{org_id}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["data"]["status"] == "approved"

    # ── 4. Org login ───────────────────────────────────────────────────────
    org_login = await client.post("/auth/org/login", json={
        "email": "ith@integration.test",
        "password": "OrgPass@123",
    })
    assert org_login.status_code == 200, org_login.text
    org_token = org_login.json()["access_token"]
    org_headers = {"Authorization": f"Bearer {org_token}"}

    # ── 5. Create service ──────────────────────────────────────────────────
    svc_resp = await client.post("/org/services", json={
        "name": "General Consultation",
        "price": 0,
        "estimated_minutes": 10,
    }, headers=org_headers)
    assert svc_resp.status_code == 200, svc_resp.text
    service_id = svc_resp.json()["data"]["id"]

    # ── 6. Create counter ──────────────────────────────────────────────────
    ctr_resp = await client.post("/org/counters", json={
        "counter_name": "OPD Counter 1",
        "counter_number": 1,
        "token_prefix": "A",
        "daily_limit": 200,
        "service_id": service_id,
    }, headers=org_headers)
    assert ctr_resp.status_code == 200, ctr_resp.text
    counter_id = ctr_resp.json()["data"]["id"]

    # ── 7. Register user ───────────────────────────────────────────────────
    user_reg = await client.post("/user/register", json={
        "name": "Integration User",
        "email": "iuser@integration.test",
        "mobile": "9000000001",
        "password": "UserPass@123",
        "confirm_password": "UserPass@123",
    })
    assert user_reg.status_code == 200, user_reg.text

    user_login = await client.post("/auth/user/login", json={
        "email": "iuser@integration.test",
        "password": "UserPass@123",
    })
    assert user_login.status_code == 200, user_login.text
    user_token = user_login.json()["access_token"]
    user_headers = {"Authorization": f"Bearer {user_token}"}

    # ── 8. User books token ────────────────────────────────────────────────
    book_resp = await client.post("/user/tokens/book", json={
        "organization_id": org_id,
        "counter_id": counter_id,
        "service_id": service_id,
    }, headers=user_headers)
    assert book_resp.status_code == 200, book_resp.text
    token_id = book_resp.json()["data"]["id"]
    assert book_resp.json()["data"]["status"] == "waiting"

    # ── 9. Org calls token ────────────────────────────────────────────────
    call_resp = await client.post(
        f"/org/tokens/{token_id}/action",
        json={"action": "call"},
        headers=org_headers,
    )
    assert call_resp.status_code == 200, call_resp.text
    assert call_resp.json()["data"]["status"] == "called"

    # ── 10. Org completes token ───────────────────────────────────────────
    complete_resp = await client.post(
        f"/org/tokens/{token_id}/action",
        json={"action": "complete"},
        headers=org_headers,
    )
    assert complete_resp.status_code == 200, complete_resp.text
    assert complete_resp.json()["data"]["status"] == "completed"

    # ── 11. User can track (terminal) ─────────────────────────────────────
    track_resp = await client.get(f"/user/tokens/{token_id}/track")
    assert track_resp.status_code == 200
    assert track_resp.json()["data"]["status"] == "completed"

    # ── 12. Token appears in user history ─────────────────────────────────
    history_resp = await client.get("/user/tokens/history", headers=user_headers)
    assert history_resp.status_code == 200
    ids = [t["id"] for t in history_resp.json()["data"]]
    assert token_id in ids
