"""tests/api/test_phase1_milestone.py
───────────────────────────────────
Comprehensive test suite verifying all Phase 1 milestone requirements:
1. Unified Authentication Principal & Tenant Context
2. RBAC & Permissions Engine (Roles, Role Mappings, require_permission)
3. Server-Side Session Tracking, Expiry & Revocation (Single & All-Device Logout)
4. Audit Logging & Credential Sanitization
5. Platform Super Admin Limit (Strict 4-Account Constraint)
6. Employee UI & Organization Management Views
7. Tenant Isolation & Object-Level Authorization
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.admin_service import (
    create_super_admin,
    deactivate_super_admin,
    get_super_admin_count,
    seed_default_admin,
    MAX_SUPER_ADMIN_ACCOUNTS,
)
from app.services.rbac_service import (
    seed_rbac_defaults,
    assign_role_to_employee,
    get_employee_permissions,
    list_roles,
)
from app.services.session_service import get_active_sessions_for_principal
from models.admin import Admin
from models.audit_log import AuditLog
from models.branch import Branch
from models.employee import Employee
from models.organization import Organization
from models.role import Role
from models.session import Session


async def _setup_org_and_branch(
    client: AsyncClient,
    db: AsyncSession,
    email: str = "orghead@example.com",
    org_name: str = "Metro Hospital",
    branch_name: str = "Main Campus",
) -> dict:
    """Helper to bootstrap an approved org, branch, and tokens."""
    await seed_default_admin(db)
    await seed_rbac_defaults(db)

    # 1. Register Org
    reg_resp = await client.post(
        "/org/register",
        json={
            "name": org_name,
            "registration_number": f"REG-{email.split('@')[0]}",
            "org_type": "hospital",
            "address": "100 Medical Blvd",
            "city": "Metro City",
            "state": "State",
            "email": email,
            "mobile": "9876543210",
            "password": "Password@123",
            "confirm_password": "Password@123",
        },
    )
    assert reg_resp.status_code == 200, reg_resp.text
    org_id = reg_resp.json()["data"]["id"]

    # 2. Admin Login & Approve
    admin_login = await client.post(
        "/auth/admin/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert admin_login.status_code == 200
    admin_token = admin_login.json()["access_token"]

    appr = await client.post(
        f"/admin/organizations/{org_id}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert appr.status_code == 200

    # 3. Org Login
    org_login = await client.post(
        "/auth/org/login",
        json={"email": email, "password": "Password@123"},
    )
    assert org_login.status_code == 200
    org_token = org_login.json()["access_token"]

    # 4. Create Branch
    b_resp = await client.post(
        "/org/branches",
        headers={"Authorization": f"Bearer {org_token}"},
        json={
            "name": branch_name,
            "branch_code": f"BR-{email.split('@')[0]}",
            "address": "100 Branch Road",
            "city": "Metro City",
            "state": "State",
            "pincode": "560001",
        },
    )
    assert b_resp.status_code in (200, 201), b_resp.text
    b_json = b_resp.json()
    branch = b_json.get("data", b_json)

    return {
        "org_id": org_id,
        "org_token": org_token,
        "admin_token": admin_token,
        "branch_id": branch["id"],
        "branch": branch,
    }


# ── 1. Unified Authentication Principal & Tenant Context ───────────────────────

@pytest.mark.asyncio
async def test_unified_principal_and_session_tracking(client: AsyncClient, db_session: AsyncSession):
    """Verify that logging in creates a server-side session and provides consistent principal context."""
    data = await _setup_org_and_branch(client, db_session, email="unified_org@example.com")
    org_token = data["org_token"]

    # Query profile using the token
    resp = await client.get("/org/api/profile", headers={"Authorization": f"Bearer {org_token}"})
    assert resp.status_code == 200
    profile = resp.json()["data"]
    assert profile["id"] == data["org_id"]
    assert profile["email"] == "unified_org@example.com"

    # Verify session exists in DB
    sessions = await get_active_sessions_for_principal(
        db_session, principal_type="organization", principal_id=data["org_id"]
    )
    assert len(sessions) >= 1
    assert sessions[0].is_revoked is False
    assert sessions[0].principal_id == data["org_id"]


# ── 2. Server-Side Session Revocation (Single & All-Device) ───────────────────

@pytest.mark.asyncio
async def test_session_revocation_on_logout(client: AsyncClient, db_session: AsyncSession):
    """Test that POST /auth/logout revokes the server session so the token cannot be reused."""
    data = await _setup_org_and_branch(client, db_session, email="session_rev@example.com")
    token = data["org_token"]

    # Endpoint works before logout
    resp_before = await client.get("/org/api/branches", headers={"Authorization": f"Bearer {token}"})
    assert resp_before.status_code == 200

    # Call logout
    logout_resp = await client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout_resp.status_code == 200
    logout_msg = logout_resp.json().get("message") or logout_resp.json().get("detail", "")
    assert "logged out" in logout_msg.lower()

    # Subsequent request using the revoked token must return 401
    resp_after = await client.get("/org/api/branches", headers={"Authorization": f"Bearer {token}"})
    assert resp_after.status_code == 401
    revoked_msg = resp_after.json().get("detail") or resp_after.json().get("message", "")
    assert "revoked" in revoked_msg.lower()


@pytest.mark.asyncio
async def test_session_logout_all(client: AsyncClient, db_session: AsyncSession):
    """Test that POST /auth/logout-all revokes all active sessions for that principal."""
    data = await _setup_org_and_branch(client, db_session, email="logout_all@example.com")

    # Second login to create a 2nd session
    login2 = await client.post(
        "/auth/org/login",
        json={"email": "logout_all@example.com", "password": "Password@123"},
    )
    token2 = login2.json()["access_token"]

    # Verify both sessions are active
    active = await get_active_sessions_for_principal(
        db_session, principal_type="organization", principal_id=data["org_id"]
    )
    assert len(active) >= 2

    # Logout all using token2
    resp = await client.post("/auth/logout-all", headers={"Authorization": f"Bearer {token2}"})
    assert resp.status_code == 200

    # Both tokens must now be rejected
    r1 = await client.get("/org/api/branches", headers={"Authorization": f"Bearer {data['org_token']}"})
    assert r1.status_code == 401
    r2 = await client.get("/org/api/branches", headers={"Authorization": f"Bearer {token2}"})
    assert r2.status_code == 401


# ── 3. RBAC & Permission Enforcement ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_rbac_role_assignment_and_permission_enforcement(client: AsyncClient, db_session: AsyncSession):
    """Test RBAC role assignment and permission enforcement on employees."""
    data = await _setup_org_and_branch(client, db_session, email="rbac_test@example.com")
    org_token = data["org_token"]
    branch_id = data["branch_id"]

    # 1. Create Employee
    emp_resp = await client.post(
        "/org/employees",
        headers={"Authorization": f"Bearer {org_token}"},
        json={
            "branch_id": branch_id,
            "name": "Operator Jane",
            "email": "jane@example.com",
            "employee_code": "OP-01",
            "password": "Password@123",
        },
    )
    assert emp_resp.status_code in (200, 201), emp_resp.text
    emp_data = emp_resp.json().get("data", emp_resp.json())
    emp_id = emp_data["id"]

    # Check available roles
    roles_resp = await client.get("/org/api/roles", headers={"Authorization": f"Bearer {org_token}"})
    assert roles_resp.status_code == 200
    roles = roles_resp.json().get("data", roles_resp.json())
    assert len(roles) >= 4  # System default roles exist

    queue_op_role = next(r for r in roles if r["code"] == "queue_operator")

    # 2. Assign queue_operator role to Jane
    assign_resp = await client.post(
        f"/org/employees/{emp_id}/roles",
        headers={"Authorization": f"Bearer {org_token}"},
        json={"role_id": queue_op_role["id"]},
    )
    assert assign_resp.status_code == 200

    # Verify permissions assigned
    emp_perms = await get_employee_permissions(db_session, emp_id)
    assert "queue:read" in emp_perms
    assert "queue:call" in emp_perms
    assert "queue:operate" in emp_perms
    assert "branches:write" not in emp_perms  # Queue operator cannot manage branches

    # 3. Employee login
    emp_login = await client.post(
        "/auth/employee/login",
        json={"email": "jane@example.com", "password": "Password@123"},
    )
    assert emp_login.status_code == 200
    emp_token = emp_login.json()["access_token"]

    # 4. Jane tries to create a branch (requires branches:write permission) -> 403 Forbidden
    unauth_branch = await client.post(
        "/org/branches",
        headers={"Authorization": f"Bearer {emp_token}"},
        json={
            "name": "Rogue Branch",
            "branch_code": "ROG-01",
            "address": "Street",
            "city": "City",
            "state": "State",
            "pincode": "560001",
        },
    )
    assert unauth_branch.status_code == 403
    unauth_detail = unauth_branch.json()["detail"].lower()
    assert "access required" in unauth_detail or "permission required" in unauth_detail


@pytest.mark.asyncio
async def test_cross_tenant_role_assignment_prevention(client: AsyncClient, db_session: AsyncSession):
    """Ensure Org A cannot assign roles to employees belonging to Org B."""
    org1 = await _setup_org_and_branch(client, db_session, email="tenant_cross_a@example.com", org_name="Cross Org A")
    org2 = await _setup_org_and_branch(client, db_session, email="tenant_cross_b@example.com", org_name="Cross Org B")

    # Org B creates an employee
    emp_resp = await client.post(
        "/org/employees",
        headers={"Authorization": f"Bearer {org2['org_token']}"},
        json={
            "branch_id": org2["branch_id"],
            "name": "Org B Staff",
            "email": "staff_b@example.com",
            "employee_code": "STAFF-B",
            "password": "Password@123",
        },
    )
    assert emp_resp.status_code in (200, 201), emp_resp.text
    emp_b_id = (emp_resp.json().get("data") or emp_resp.json())["id"]

    roles_resp = await client.get("/org/api/roles", headers={"Authorization": f"Bearer {org1['org_token']}"})
    roles = roles_resp.json().get("data", roles_resp.json())
    role_id = roles[0]["id"]

    # Org A tries to assign role to Org B's employee
    rogue_assign = await client.post(
        f"/org/employees/{emp_b_id}/roles",
        headers={"Authorization": f"Bearer {org1['org_token']}"},
        json={"role_id": role_id},
    )
    assert rogue_assign.status_code in (400, 404)
    assert "Employee not found" in rogue_assign.json()["detail"]


# ── 4. Platform Super Admin Limit (Strict 4-Account Constraint) ────────────────

@pytest.mark.asyncio
async def test_super_admin_four_account_limit(db_session: AsyncSession):
    """Verify Platform Super Admin 4-account limit, deactivation, and recovery."""
    await seed_default_admin(db_session)
    count = await get_super_admin_count(db_session, active_only=True)
    assert count == 1

    # Provision 3 more admins (total 4)
    a2 = await create_super_admin(db_session, "Admin Two", "admin2@skipline.io", "Admin@12345")
    a3 = await create_super_admin(db_session, "Admin Three", "admin3@skipline.io", "Admin@12345")
    a4 = await create_super_admin(db_session, "Admin Four", "admin4@skipline.io", "Admin@12345")

    total_active = await get_super_admin_count(db_session, active_only=True)
    assert total_active == 4

    # Attempting to provision a 5th Super Admin MUST fail with ValueError
    with pytest.raises(ValueError) as excinfo:
        await create_super_admin(db_session, "Admin Five", "admin5@skipline.io", "Admin@12345")
    assert f"Exactly {MAX_SUPER_ADMIN_ACCOUNTS} authorized" in str(excinfo.value)

    # Deactivating one admin frees up a slot
    await deactivate_super_admin(db_session, a4.id)
    assert await get_super_admin_count(db_session, active_only=True) == 3

    # Now provisioning a replacement succeeds
    a5 = await create_super_admin(db_session, "Admin Replacement", "admin_rep@skipline.io", "Admin@12345")
    assert a5.id is not None
    assert await get_super_admin_count(db_session, active_only=True) == 4


# ── 5. Audit Logging & Security Sanitization ──────────────────────────────────

@pytest.mark.asyncio
async def test_audit_logging_and_credential_sanitization(client: AsyncClient, db_session: AsyncSession):
    """Verify sensitive administrative and authentication actions create sanitized audit entries."""
    data = await _setup_org_and_branch(client, db_session, email="audit_demo@example.com")
    admin_token = data["admin_token"]
    org_token = data["org_token"]

    # 1. Admin login created audit entry
    res = await db_session.execute(
        select(AuditLog).where(AuditLog.action.in_(["auth.admin.login", "ADMIN_LOGIN"]))
    )
    admin_log = res.scalars().first()
    assert admin_log is not None
    assert admin_log.actor_type == "admin"
    assert "password" not in (admin_log.details or "")

    # 2. Org Head creates an employee -> creates audit entry
    emp_resp = await client.post(
        "/org/employees",
        headers={"Authorization": f"Bearer {org_token}"},
        json={
            "branch_id": data["branch_id"],
            "name": "Audit Staff",
            "email": "audit_staff@example.com",
            "employee_code": "ASTAFF-01",
            "password": "SecretStaffPassword!123",
        },
    )
    assert emp_resp.status_code in (200, 201), emp_resp.text

    res_emp = await db_session.execute(
        select(AuditLog).where(
            AuditLog.action.in_(["employee.create", "EMPLOYEE_CREATE"]),
            AuditLog.actor_email == "audit_demo@example.com",
        )
    )
    emp_log = res_emp.scalars().first()
    assert emp_log is not None
    assert emp_log.target_type == "employee"
    assert "SecretStaffPassword!123" not in (emp_log.details or "")
    assert "password" not in (emp_log.details or "")

    # 3. Query audit logs via Org API (ensuring tenant isolation)
    logs_resp = await client.get("/org/api/audit-logs", headers={"Authorization": f"Bearer {org_token}"})
    assert logs_resp.status_code == 200
    logs = logs_resp.json().get("data", logs_resp.json())
    assert len(logs) >= 1
    for log in logs:
        assert log["organization_id"] == data["org_id"]  # Only sees their own logs


# ── 6. Dedicated Employee UI & Org Staff Views ────────────────────────────────

@pytest.mark.asyncio
async def test_employee_ui_views_and_flow(client: AsyncClient, db_session: AsyncSession):
    """Test dedicated Employee UI login, form submission, cookie auth, dashboard, and logout."""
    data = await _setup_org_and_branch(client, db_session, email="empui_org@example.com")
    org_token = data["org_token"]

    # Create active employee
    emp_resp = await client.post(
        "/org/employees",
        headers={"Authorization": f"Bearer {org_token}"},
        json={
            "branch_id": data["branch_id"],
            "name": "Desk Officer Bob",
            "email": "bob@example.com",
            "employee_code": "BOB-01",
            "password": "Password@123",
        },
    )
    assert emp_resp.status_code in (200, 201), emp_resp.text

    # 1. GET /employee/login (HTML View)
    get_login = await client.get("/employee/login")
    assert get_login.status_code == 200
    assert "Staff & Counter Operator" in get_login.text or "Staff" in get_login.text
    assert "action=\"/employee/login/submit\"" in get_login.text

    # 2. POST /employee/login/submit with invalid credentials -> redirects with error
    bad_login = await client.post(
        "/employee/login/submit",
        data={"email": "bob@example.com", "password": "WrongPassword"},
        follow_redirects=False,
    )
    assert bad_login.status_code in (302, 303)
    assert "error" in bad_login.headers["location"]

    # 3. POST /employee/login/submit with valid credentials -> sets cookie & redirects to dashboard
    good_login = await client.post(
        "/employee/login/submit",
        data={"email": "bob@example.com", "password": "Password@123"},
        follow_redirects=False,
    )
    assert good_login.status_code in (302, 303)
    assert good_login.headers["location"] == "/employee/dashboard"
    assert "employee_token" in good_login.cookies

    # 4. GET /employee/dashboard with cookie -> renders station dashboard
    dash = await client.get("/employee/dashboard", cookies=good_login.cookies)
    assert dash.status_code == 200
    assert "Desk Officer Bob" in dash.text
    assert "Main Campus" in dash.text

    # 5. GET /employee/logout -> clears cookie and redirects to /employee/login
    logout = await client.get("/employee/logout", cookies=good_login.cookies, follow_redirects=False)
    assert logout.status_code in (302, 303)
    assert logout.headers["location"] == "/employee/login"


@pytest.mark.asyncio
async def test_org_management_views_render(client: AsyncClient, db_session: AsyncSession):
    """Test that Organization Head management HTML views for branches and staff render properly."""
    data = await _setup_org_and_branch(client, db_session, email="orgviews@example.com")
    cookies = {"org_token": data["org_token"]}

    # GET /org/branches
    branches_view = await client.get("/org/branches", cookies=cookies)
    assert branches_view.status_code == 200
    assert "Physical Branches" in branches_view.text or "Branches" in branches_view.text
    assert "Add Branch" in branches_view.text

    # GET /org/employees
    employees_view = await client.get("/org/employees", cookies=cookies)
    assert employees_view.status_code == 200
    assert "Staff & Employee Management" in employees_view.text
    assert "Add Staff Member" in employees_view.text
