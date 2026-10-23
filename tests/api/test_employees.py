"""tests/api/test_employees.py — Employee accounts, dedicated authentication, and tenant isolation tests."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.admin_service import seed_default_admin
from app.utils.jwt import decode_token


async def _register_and_approve_org(
    client: AsyncClient,
    db_session: AsyncSession,
    email: str,
    name: str,
    reg_num: str,
) -> tuple[int, str]:
    """Helper to seed admin, register an org, approve it, and return (org_id, access_token)."""
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
    return org_id, org_login.json()["access_token"]


async def _create_branch(client: AsyncClient, org_token: str, name: str, code: str) -> int:
    """Helper to create a branch for an organization."""
    resp = await client.post(
        "/org/branches",
        json={
            "name": name,
            "branch_code": code,
            "address": "Floor 1, Building A",
            "city": "Metro City",
            "state": "State",
            "pincode": "560001",
            "is_active": True,
        },
        headers={"Authorization": f"Bearer {org_token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


@pytest.mark.api
async def test_successful_employee_creation(client: AsyncClient, db_session: AsyncSession):
    """Scenario 1: Org head successfully creates an employee; verifies response and no password_hash leak."""
    org_id, org_token = await _register_and_approve_org(
        client, db_session, "emp_org1@example.com", "Emp Hospital", "EH-001"
    )
    branch_id = await _create_branch(client, org_token, "Main Branch", "MB-01")

    create_resp = await client.post(
        "/org/employees",
        json={
            "name": "Alice Operator",
            "email": "alice.operator@example.com",
            "employee_code": "EMP-001",
            "branch_id": branch_id,
            "password": "SecurePassword@123",
            "is_active": True,
        },
        headers={"Authorization": f"Bearer {org_token}"},
    )
    assert create_resp.status_code == 200, create_resp.text
    data = create_resp.json()["data"]

    assert data["name"] == "Alice Operator"
    assert data["email"] == "alice.operator@example.com"
    assert data["employee_code"] == "EMP-001"
    assert data["organization_id"] == org_id
    assert data["branch_id"] == branch_id
    assert data["is_active"] is True
    assert "id" in data
    assert "created_at" in data
    assert "updated_at" in data
    # Security: ensure password_hash is never exposed
    assert "password_hash" not in data
    assert "password" not in data


@pytest.mark.api
async def test_successful_employee_login(client: AsyncClient, db_session: AsyncSession):
    """Scenario 2: Employee logs in via dedicated endpoint; verifies token payload and claims."""
    org_id, org_token = await _register_and_approve_org(
        client, db_session, "emp_org2@example.com", "Login Clinic", "LC-002"
    )
    branch_id = await _create_branch(client, org_token, "North Branch", "NB-01")

    create_resp = await client.post(
        "/org/employees",
        json={
            "name": "Bob Cashier",
            "email": "bob.cashier@example.com",
            "employee_code": "BOB-01",
            "branch_id": branch_id,
            "password": "Password123!",
            "is_active": True,
        },
        headers={"Authorization": f"Bearer {org_token}"},
    )
    assert create_resp.status_code == 200
    emp_id = create_resp.json()["data"]["id"]

    # Dedicated employee login
    login_resp = await client.post(
        "/auth/employee/login",
        json={"email": "bob.cashier@example.com", "password": "Password123!"},
    )
    assert login_resp.status_code == 200, login_resp.text
    body = login_resp.json()

    assert body["role"] == "employee"
    assert body["entity_id"] == emp_id
    assert body["organization_id"] == org_id
    assert body["branch_id"] == branch_id
    assert body["name"] == "Bob Cashier"
    assert body["email"] == "bob.cashier@example.com"
    assert "access_token" in body

    # Verify JWT payload claims
    token = body["access_token"]
    payload = decode_token(token)
    assert payload is not None
    assert payload["sub"] == str(emp_id)
    assert payload["role"] == "employee"
    assert payload["org_id"] == org_id
    assert payload["branch_id"] == branch_id

    # Verify employee_token cookie was set
    assert "employee_token" in login_resp.cookies


@pytest.mark.api
async def test_invalid_employee_password(client: AsyncClient, db_session: AsyncSession):
    """Scenario 3: Employee login with invalid password returns 401 safely."""
    _, org_token = await _register_and_approve_org(
        client, db_session, "emp_org3@example.com", "Invalid Pwd Org", "IPO-003"
    )
    branch_id = await _create_branch(client, org_token, "Branch A", "BA-01")

    await client.post(
        "/org/employees",
        json={
            "name": "Carol Staff",
            "email": "carol@example.com",
            "employee_code": "CAROL-01",
            "branch_id": branch_id,
            "password": "CorrectPassword@123",
        },
        headers={"Authorization": f"Bearer {org_token}"},
    )

    # Attempt login with wrong password
    bad_login = await client.post(
        "/auth/employee/login",
        json={"email": "carol@example.com", "password": "WrongPassword!"},
    )
    assert bad_login.status_code == 401
    assert "invalid" in bad_login.json()["detail"].lower()

    # Attempt login with non-existent email
    ghost_login = await client.post(
        "/auth/employee/login",
        json={"email": "nonexistent@example.com", "password": "WrongPassword!"},
    )
    assert ghost_login.status_code == 401
    assert "invalid" in ghost_login.json()["detail"].lower()


@pytest.mark.api
async def test_inactive_employee_rejection(client: AsyncClient, db_session: AsyncSession):
    """Scenario 4: Inactive employee account cannot login and existing tokens are rejected."""
    _, org_token = await _register_and_approve_org(
        client, db_session, "emp_org4@example.com", "Inactive Test Org", "ITO-004"
    )
    branch_id = await _create_branch(client, org_token, "Branch Inactive", "BI-01")

    create_resp = await client.post(
        "/org/employees",
        json={
            "name": "Dave Inactive",
            "email": "dave@example.com",
            "employee_code": "DAVE-01",
            "branch_id": branch_id,
            "password": "Password@123",
            "is_active": True,
        },
        headers={"Authorization": f"Bearer {org_token}"},
    )
    emp_id = create_resp.json()["data"]["id"]

    # Login while active to obtain token
    active_login = await client.post(
        "/auth/employee/login",
        json={"email": "dave@example.com", "password": "Password@123"},
    )
    assert active_login.status_code == 200
    token = active_login.json()["access_token"]

    # Deactivate employee via org head
    deact_resp = await client.put(
        f"/org/employees/{emp_id}",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {org_token}"},
    )
    assert deact_resp.status_code == 200
    assert deact_resp.json()["data"]["is_active"] is False

    # Login attempt must now fail
    inactive_login = await client.post(
        "/auth/employee/login",
        json={"email": "dave@example.com", "password": "Password@123"},
    )
    assert inactive_login.status_code == 401
    assert "inactive" in inactive_login.json()["detail"].lower() or "invalid" in inactive_login.json()["detail"].lower()

    # Request with pre-existing token must also be rejected by dependency
    from app.dependencies import get_current_employee
    from starlette.requests import Request
    from fastapi.security import HTTPAuthorizationCredentials

    req = Request(scope={"type": "http", "headers": []})
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with pytest.raises(Exception) as exc_info:
        await get_current_employee(req, credentials=creds, db=db_session)
    assert "inactive" in str(exc_info.value.detail).lower()


@pytest.mark.api
async def test_employee_assigned_to_branch_from_another_org(client: AsyncClient, db_session: AsyncSession):
    """Scenario 5: Org A cannot assign an employee to Org B's branch."""
    _, token_a = await _register_and_approve_org(
        client, db_session, "tenant_a_emp@example.com", "Tenant A Hospital", "TEA-001"
    )
    _, token_b = await _register_and_approve_org(
        client, db_session, "tenant_b_emp@example.com", "Tenant B Clinic", "TEB-001"
    )

    branch_b_id = await _create_branch(client, token_b, "Branch of Org B", "BOB-01")

    # Org A attempts to create an employee pointing to Org B's branch
    bad_create = await client.post(
        "/org/employees",
        json={
            "name": "Intruder Staff",
            "email": "intruder@example.com",
            "employee_code": "INT-01",
            "branch_id": branch_b_id,
            "password": "Password@123",
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert bad_create.status_code == 400
    assert "belongs to another organization" in bad_create.json()["detail"].lower() or "not found" in bad_create.json()["detail"].lower()


@pytest.mark.api
async def test_cross_tenant_employee_isolation(client: AsyncClient, db_session: AsyncSession):
    """Scenario 6: Cross-tenant access isolation: Org B cannot access, mutate, or delete Org A's employees."""
    _, token_a = await _register_and_approve_org(
        client, db_session, "iso_a@example.com", "Iso Org A", "ISO-A"
    )
    _, token_b = await _register_and_approve_org(
        client, db_session, "iso_b@example.com", "Iso Org B", "ISO-B"
    )

    branch_a_id = await _create_branch(client, token_a, "Branch A", "BA-01")

    # Org A creates employee
    create_resp = await client.post(
        "/org/employees",
        json={
            "name": "Secret Agent A",
            "email": "agent_a@example.com",
            "employee_code": "AGENT-01",
            "branch_id": branch_a_id,
            "password": "Password@123",
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert create_resp.status_code == 200
    emp_a_id = create_resp.json()["data"]["id"]

    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Org B attempts to read Org A's employee -> 404
    b_get = await client.get(f"/org/employees/{emp_a_id}", headers=headers_b)
    assert b_get.status_code == 404

    # Org B attempts to update Org A's employee -> 404
    b_put = await client.put(
        f"/org/employees/{emp_a_id}",
        json={"name": "Hacked Name"},
        headers=headers_b,
    )
    assert b_put.status_code == 404

    # Org B attempts to delete Org A's employee -> 404
    b_del = await client.delete(f"/org/employees/{emp_a_id}", headers=headers_b)
    assert b_del.status_code == 404

    # Org B listing must NOT leak Org A's employee
    b_list = await client.get("/org/api/employees", headers=headers_b)
    assert b_list.status_code == 200
    assert len(b_list.json()["data"]) == 0


@pytest.mark.api
async def test_employee_cannot_access_org_head_endpoints(client: AsyncClient, db_session: AsyncSession):
    """Scenario 7: Employee principal receives 403 Forbidden on org-head-only and admin endpoints."""
    _, org_token = await _register_and_approve_org(
        client, db_session, "boundary_org@example.com", "Boundary Hospital", "BOU-001"
    )
    branch_id = await _create_branch(client, org_token, "Main Desk", "MD-01")

    await client.post(
        "/org/employees",
        json={
            "name": "Front Desk Employee",
            "email": "frontdesk@example.com",
            "employee_code": "FD-01",
            "branch_id": branch_id,
            "password": "Password@123",
        },
        headers={"Authorization": f"Bearer {org_token}"},
    )

    login_resp = await client.post(
        "/auth/employee/login",
        json={"email": "frontdesk@example.com", "password": "Password@123"},
    )
    emp_token = login_resp.json()["access_token"]
    emp_headers = {"Authorization": f"Bearer {emp_token}"}

    # Attempting org-head endpoints with employee token must return 403 Forbidden
    r_profile = await client.get("/org/api/profile", headers=emp_headers)
    assert r_profile.status_code == 403
    assert "organization access required" in r_profile.json()["detail"].lower()

    r_branches = await client.get("/org/api/branches", headers=emp_headers)
    assert r_branches.status_code == 403

    r_post_branch = await client.post(
        "/org/branches",
        json={
            "name": "Unauthorized Branch",
            "branch_code": "UNAUTH",
            "address": "Nowhere",
            "city": "City",
            "state": "State",
            "pincode": "123456",
        },
        headers=emp_headers,
    )
    assert r_post_branch.status_code == 403

    r_employees = await client.get("/org/api/employees", headers=emp_headers)
    assert r_employees.status_code == 403

    r_post_emp = await client.post(
        "/org/employees",
        json={
            "name": "Unauthorized Emp",
            "email": "unauth@example.com",
            "employee_code": "UNAUTH-01",
            "branch_id": branch_id,
            "password": "Password@123",
        },
        headers=emp_headers,
    )
    assert r_post_emp.status_code == 403

    # Attempting platform admin endpoint with employee token must return 403 Forbidden
    r_admin = await client.get("/admin/api/organizations", headers=emp_headers)
    assert r_admin.status_code == 403
    assert "admin access required" in r_admin.json()["detail"].lower()


@pytest.mark.api
async def test_existing_org_head_auth_still_works(client: AsyncClient, db_session: AsyncSession):
    """Scenario 8: Existing organization head authentication and endpoints remain completely intact."""
    _, org_token = await _register_and_approve_org(
        client, db_session, "head_verify@example.com", "Head Hospital", "HH-001"
    )
    headers = {"Authorization": f"Bearer {org_token}"}

    # Org profile API
    prof = await client.get("/org/api/profile", headers=headers)
    assert prof.status_code == 200
    assert prof.json()["data"]["email"] == "head_verify@example.com"

    # Org branches API
    b_resp = await client.get("/org/api/branches", headers=headers)
    assert b_resp.status_code == 200

    # Org settings API
    s_resp = await client.get("/org/api/settings", headers=headers)
    assert s_resp.status_code == 200


@pytest.mark.api
async def test_existing_branch_functionality_still_works(client: AsyncClient, db_session: AsyncSession):
    """Scenario 9: Branch creation, retrieval, listing, updating, and deletion remain intact."""
    _, org_token = await _register_and_approve_org(
        client, db_session, "branch_verify@example.com", "Branch Check Clinic", "BCC-001"
    )
    headers = {"Authorization": f"Bearer {org_token}"}

    # Create branch
    b_id = await _create_branch(client, org_token, "Branch Regression Check", "BRC-01")

    # Get branch
    get_resp = await client.get(f"/org/branches/{b_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["data"]["branch_code"] == "BRC-01"

    # List branches
    list_resp = await client.get("/org/api/branches", headers=headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()["data"]) == 1

    # Update branch
    put_resp = await client.put(
        f"/org/branches/{b_id}",
        json={"name": "Updated Branch Name"},
        headers=headers,
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["data"]["name"] == "Updated Branch Name"


@pytest.mark.api
async def test_employee_crud_lifecycle_and_filters(client: AsyncClient, db_session: AsyncSession):
    """Scenario 10: Complete employee lifecycle: multiple branches, filters, uniqueness within org, delete."""
    _, org_token = await _register_and_approve_org(
        client, db_session, "emp_life@example.com", "Life Hospital", "LH-001"
    )
    headers = {"Authorization": f"Bearer {org_token}"}

    b1_id = await _create_branch(client, org_token, "Branch 1", "B1")
    b2_id = await _create_branch(client, org_token, "Branch 2", "B2")

    # Create 2 employees in branch 1 and 1 in branch 2
    e1_resp = await client.post(
        "/org/employees",
        json={"name": "Emp 1", "email": "e1@example.com", "employee_code": "CODE-1", "branch_id": b1_id, "password": "Password@123"},
        headers=headers,
    )
    assert e1_resp.status_code == 200
    e1_id = e1_resp.json()["data"]["id"]

    e2_resp = await client.post(
        "/org/employees",
        json={"name": "Emp 2", "email": "e2@example.com", "employee_code": "CODE-2", "branch_id": b1_id, "password": "Password@123", "is_active": False},
        headers=headers,
    )
    assert e2_resp.status_code == 200

    e3_resp = await client.post(
        "/org/employees",
        json={"name": "Emp 3", "email": "e3@example.com", "employee_code": "CODE-3", "branch_id": b2_id, "password": "Password@123"},
        headers=headers,
    )
    assert e3_resp.status_code == 200

    # Filter by branch
    list_b1 = await client.get(f"/org/api/employees?branch_id={b1_id}", headers=headers)
    assert list_b1.status_code == 200
    assert len(list_b1.json()["data"]) == 2

    # Filter by active only
    list_active = await client.get("/org/api/employees?active_only=true", headers=headers)
    assert list_active.status_code == 200
    assert len(list_active.json()["data"]) == 2

    # Duplicate code within same org rejected
    dup_code = await client.post(
        "/org/employees",
        json={"name": "Dup Emp", "email": "dup@example.com", "employee_code": "CODE-1", "branch_id": b1_id, "password": "Password@123"},
        headers=headers,
    )
    assert dup_code.status_code == 400
    assert "already in use" in dup_code.json()["detail"].lower()

    # Duplicate email rejected
    dup_email = await client.post(
        "/org/employees",
        json={"name": "Dup Email", "email": "e1@example.com", "employee_code": "UNIQUE-CODE", "branch_id": b1_id, "password": "Password@123"},
        headers=headers,
    )
    assert dup_email.status_code == 400
    assert "already registered" in dup_email.json()["detail"].lower()

    # Move employee 1 to branch 2
    move_resp = await client.put(
        f"/org/employees/{e1_id}",
        json={"branch_id": b2_id, "name": "Emp 1 Relocated"},
        headers=headers,
    )
    assert move_resp.status_code == 200
    assert move_resp.json()["data"]["branch_id"] == b2_id
    assert move_resp.json()["data"]["name"] == "Emp 1 Relocated"

    # Delete employee 1
    del_resp = await client.delete(f"/org/employees/{e1_id}", headers=headers)
    assert del_resp.status_code == 200

    # Verify deleted
    get_del = await client.get(f"/org/employees/{e1_id}", headers=headers)
    assert get_del.status_code == 404
