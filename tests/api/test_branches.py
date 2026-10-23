"""tests/api/test_branches.py — Physical branch management and tenant isolation tests."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.admin_service import seed_default_admin


async def _register_and_approve_org(
    client: AsyncClient,
    db_session: AsyncSession,
    email: str,
    name: str,
    reg_num: str,
) -> str:
    """Helper to seed admin, register an org, approve it, and return its access token."""
    await seed_default_admin(db_session)

    # 1. Register Org
    resp = await client.post(
        "/org/register",
        json={
            "name": name,
            "registration_number": reg_num,
            "org_type": "hospital",
            "address": "123 Health Ave",
            "city": "Metro City",
            "state": "State",
            "email": email,
            "mobile": "9876543210",
            "password": "Password@123",
            "confirm_password": "Password@123",
        },
    )
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["data"]["id"]

    # 2. Admin Login & Approve
    admin_login = await client.post(
        "/auth/admin/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert admin_login.status_code == 200
    admin_token = admin_login.json()["access_token"]

    approve_resp = await client.post(
        f"/admin/organizations/{org_id}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert approve_resp.status_code == 200

    # 3. Org Login
    org_login = await client.post(
        "/auth/org/login",
        json={"email": email, "password": "Password@123"},
    )
    assert org_login.status_code == 200
    return org_login.json()["access_token"]


@pytest.mark.api
async def test_org_branch_lifecycle(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_approve_org(
        client, db_session, "orgbranch@example.com", "Branch Hospital", "BH-001"
    )
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Create branch
    create_resp = await client.post(
        "/org/branches",
        json={
            "name": "Central OPD Branch",
            "branch_code": "CENTRAL-01",
            "address": "Block A, 1st Floor",
            "city": "Metro City",
            "state": "State",
            "pincode": "560001",
            "is_active": True,
        },
        headers=headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    data = create_resp.json()["data"]
    branch_id = data["id"]
    assert data["name"] == "Central OPD Branch"
    assert data["branch_code"] == "CENTRAL-01"

    # 2. List branches
    list_resp = await client.get("/org/api/branches", headers=headers)
    assert list_resp.status_code == 200
    branches = list_resp.json()["data"]
    assert len(branches) == 1
    assert branches[0]["id"] == branch_id

    # 3. Get single branch
    get_resp = await client.get(f"/org/branches/{branch_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["data"]["branch_code"] == "CENTRAL-01"

    # 4. Update branch
    update_resp = await client.put(
        f"/org/branches/{branch_id}",
        json={"name": "Central OPD Main Block"},
        headers=headers,
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["data"]["name"] == "Central OPD Main Block"

    # 5. Delete branch
    del_resp = await client.delete(f"/org/branches/{branch_id}", headers=headers)
    assert del_resp.status_code == 200

    # 6. Verify deleted
    verify_resp = await client.get(f"/org/branches/{branch_id}", headers=headers)
    assert verify_resp.status_code == 404


@pytest.mark.api
async def test_cross_tenant_branch_isolation(client: AsyncClient, db_session: AsyncSession):
    """Verify that Organization B cannot access, mutate, or delete Organization A's branch."""
    token_a = await _register_and_approve_org(
        client, db_session, "tenant_a@example.com", "Tenant A Hospital", "TA-001"
    )
    token_b = await _register_and_approve_org(
        client, db_session, "tenant_b@example.com", "Tenant B Clinic", "TB-001"
    )

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Org A creates a branch
    create_resp = await client.post(
        "/org/branches",
        json={
            "name": "Branch Alpha",
            "branch_code": "ALPHA-1",
            "address": "Alpha Road",
            "city": "City Alpha",
            "state": "State",
            "pincode": "123456",
        },
        headers=headers_a,
    )
    assert create_resp.status_code == 200
    branch_a_id = create_resp.json()["data"]["id"]

    # Org B attempts to read Org A's branch
    b_get = await client.get(f"/org/branches/{branch_a_id}", headers=headers_b)
    assert b_get.status_code == 404

    # Org B attempts to update Org A's branch
    b_put = await client.put(
        f"/org/branches/{branch_a_id}",
        json={"name": "Hacked Branch"},
        headers=headers_b,
    )
    assert b_put.status_code == 404

    # Org B attempts to delete Org A's branch
    b_del = await client.delete(f"/org/branches/{branch_a_id}", headers=headers_b)
    assert b_del.status_code == 404

    # Org B's branch list must not leak Org A's branch
    b_list = await client.get("/org/api/branches", headers=headers_b)
    assert b_list.status_code == 200
    assert len(b_list.json()["data"]) == 0


@pytest.mark.api
async def test_branch_code_uniqueness_per_tenant(client: AsyncClient, db_session: AsyncSession):
    """Duplicate codes within same org are rejected; same code across different orgs is allowed."""
    token_a = await _register_and_approve_org(
        client, db_session, "unique_a@example.com", "Org Alpha", "UA-001"
    )
    token_b = await _register_and_approve_org(
        client, db_session, "unique_b@example.com", "Org Beta", "UB-001"
    )

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    payload = {
        "name": "Branch Main",
        "branch_code": "MAIN",
        "address": "123 Main St",
        "city": "City",
        "state": "State",
        "pincode": "123456",
    }

    # Org A creates with code MAIN
    r1 = await client.post("/org/branches", json=payload, headers=headers_a)
    assert r1.status_code == 200

    # Org A attempts duplicate with code MAIN -> 400
    r2 = await client.post("/org/branches", json=payload, headers=headers_a)
    assert r2.status_code == 400
    assert "already in use" in r2.json()["detail"]

    # Org B creates with same code MAIN -> 200 OK (different tenant)
    r3 = await client.post("/org/branches", json=payload, headers=headers_b)
    assert r3.status_code == 200


@pytest.mark.api
async def test_branch_validation_and_auth_failures(client: AsyncClient):
    # 1. Unauthenticated request -> 401
    resp = await client.get("/org/api/branches")
    assert resp.status_code == 401

    # 2. Invalid schema (missing branch_code, address, etc.)
    login_resp = await client.post(
        "/org/register",
        json={
            "name": "Auth Test Org",
            "registration_number": "ATO-001",
            "org_type": "bank",
            "address": "Bank St",
            "city": "Bankville",
            "state": "State",
            "email": "ato@example.com",
            "mobile": "9999999999",
            "password": "Password@123",
            "confirm_password": "Password@123",
        },
    )
    # Login as user instead
    user_reg = await client.post(
        "/user/register",
        json={
            "name": "Ordinary User",
            "email": "ordinary@example.com",
            "mobile": "9999999998",
            "password": "Password@123",
            "confirm_password": "Password@123",
        },
    )
    user_login = await client.post(
        "/auth/user/login",
        json={"email": "ordinary@example.com", "password": "Password@123"},
    )
    user_token = user_login.json()["access_token"]

    # User token cannot access org branches -> 403 Forbidden
    forbidden_resp = await client.get(
        "/org/api/branches",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert forbidden_resp.status_code == 403
