"""
tests/api/test_counter_shifts.py
───────────────────────────────
SHIFT-001/002 — Daily counter shift state machine:
- Employee open request (counter, services, device readiness, opening
  cash, remarks) creates an OPEN_REQUESTED (never approved/active) shift.
- Only the organization head (or an employee explicitly delegated via the
  `shifts:approve` RBAC permission) can decide; both directions require a
  reason; approvals record approved period, services and payment
  permission; rejections keep the row pending for correction.
- Activation of one's own approved, same-tenant-today shift; a pending
  shift can never be activated; the require_active_shift guard (SHIFT-003
  primitive / Release Gate 4) only returns a shift after approval plus
  activation.
- Strict tenant isolation: foreign-tenant counters, services and shifts
  are indistinguishable from missing resources.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import (
    employee_service,
    org_service,
    rbac_service,
    shift_service,
)
from app.services.admin_service import seed_default_admin
from app.utils.jwt import create_access_token
from models.branch import Branch
from models.counter import Counter, CounterStatus
from models.counter_shift import CounterShiftStatus
from models.employee_role_assignment import EmployeeRoleAssignment
from models.organization import Organization
from models.role import Role
from models.role_permission import RolePermission
from models.service import Service
from models.permission import Permission
from models.counter_shift import CounterShift  # noqa: F401


# ── Fixtures ──────────────────────────────────────────────────────────────────


async def _make_org(db_session: AsyncSession) -> Organization:
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(db_session, {
        "name": f"Shift Test Org {uid}",
        "registration_number": f"STO-{uid}",
        "org_type": "hospital",
        "email": f"shift_{uid}@hospital.com",
        "mobile": "9876543210",
        "password": "Password123!",
        "address": "100 Health Way",
        "city": "Bengaluru",
        "state": "Karnataka",
    })
    org.status = "approved"
    await db_session.commit()
    await db_session.refresh(org)
    return org


async def _make_counter(
    db_session: AsyncSession,
    org: Organization,
    name: str = "Counter 1",
    status: CounterStatus = CounterStatus.ACTIVE,
) -> Counter:
    counter = Counter(
        organization_id=org.id,
        counter_name=name,
        counter_number=1,
        token_prefix="S",
        daily_limit=100,
        status=status,
    )
    db_session.add(counter)
    await db_session.commit()
    await db_session.refresh(counter)
    return counter


async def _make_employee(db_session: AsyncSession, org: Organization, code: str):
    branch = Branch(
        organization_id=org.id,
        name=f"Branch {code}",
        branch_code=code,
        address="Floor 1",
        city="Bengaluru",
        state="Karnataka",
        pincode="560001",
    )
    db_session.add(branch)
    await db_session.flush()
    employee = await employee_service.create_employee(db_session, org.id, {
        "name": f"Operator {code}",
        "email": f"operator_{code}_{uuid.uuid4().hex[:6]}@example.com",
        "employee_code": code,
        "branch_id": branch.id,
        "password": "SecurePassword@123",
    })
    return employee


def employee_headers(employee) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(employee.id, 'employee')}"}


def org_headers(org: Organization) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(org.id, 'organization')}"}


# ── Core fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
async def org_a(db_session: AsyncSession) -> Organization:
    return await _make_org(db_session)


@pytest.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    return await _make_org(db_session)


@pytest.fixture
async def operator(db_session: AsyncSession, org_a: Organization):
    return await _make_employee(db_session, org_a, f"OP-{uuid.uuid4().hex[:6]}")


@pytest.fixture
async def counter_a(db_session: AsyncSession, org_a: Organization) -> Counter:
    return await _make_counter(db_session, org_a)


@pytest.fixture
async def service_a(db_session: AsyncSession, org_a: Organization) -> Service:
    service = Service(
        organization_id=org_a.id,
        name=f"Service {uuid.uuid4().hex[:6]}",
        price=Decimal("100.00"),
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    return service


@pytest.fixture
async def delegated_manager(db_session: AsyncSession, org_a: Organization):
    """Employee explicitly delegated for shift decisions (SHIFT-002)."""
    employee = await _make_employee(db_session, org_a, f"MG-{uuid.uuid4().hex[:6]}")
    await rbac_service.seed_rbac_defaults(db_session)
    from sqlalchemy import select as sa_select
    perm = (
        await db_session.execute(
            sa_select(Permission).where(Permission.code == "shifts:approve")
        )
    ).scalar_one()
    role = Role(
        organization_id=org_a.id,
        code=f"shift_manager_{uuid.uuid4().hex[:6]}",
        name="Shift Approvals",
        is_system=False,
    )
    db_session.add(role)
    await db_session.flush()
    db_session.add(RolePermission(role_id=role.id, permission_id=perm.id))
    db_session.add(
        EmployeeRoleAssignment(employee_id=employee.id, role_id=role.id)
    )
    await db_session.commit()
    await db_session.refresh(employee)
    return employee


def open_payload(counter_id: int, service_id: int | None = None) -> dict:
    payload = {
        "counter_id": counter_id,
        "services": [service_id] if service_id else [],
        "device_ready": True,
        "opening_cash": "500.00",
        "remarks": "Morning shift",
    }
    return payload


# ── SHIFT-001: open request ──────────────────────────────────────────────────


@pytest.mark.api
async def test_open_request_creates_pending_shift_never_active(
    client: AsyncClient,
    org_a: Organization,
    operator,
    counter_a: Counter,
    service_a: Service,
    db_session: AsyncSession,
):
    """SHIFT-001 + SHIFT-002 gate: submission stays pending and can never
    land in APPROVED/ACTIVE without a head decision."""
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id, service_a.id),
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["status"] == "open_requested"
    assert data["status"] != "approved" and data["status"] != "active"
    assert data["counter_id"] == counter_a.id
    assert data["employee_id"] == operator.id
    assert data["requested_services"] == [service_a.id]
    assert data["device_ready"] is True
    assert Decimal(data["opening_cash"]) == Decimal("500.00")
    assert data["remarks"] == "Morning shift"


@pytest.mark.api
async def test_open_request_foreign_counter_is_404_and_not_persisted(
    client: AsyncClient,
    org_a: Organization,
    org_b: Organization,
    operator,
    db_session: AsyncSession,
):
    """Tenant isolation: another organization's counter is not found."""
    foreign_counter = await _make_counter(db_session, org_b)
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(foreign_counter.id),
    )
    assert res.status_code == 404
    # The app's global 404 handler normalizes the body across routes
    # (indistinguishable missing resources); the status code is the contract.
    from sqlalchemy import select as sa_select
    from models.counter_shift import CounterShift as CounterShiftModel
    rows = (
        await db_session.execute(
            sa_select(CounterShiftModel).where(CounterShiftModel.employee_id == operator.id)
        )
    ).scalars().all()
    assert len(rows) == 0


def db_import_counter_shift_filter(employee_id: int):
    from sqlalchemy import select
    from models.counter_shift import CounterShift

    return select(CounterShift).where(CounterShift.employee_id == employee_id)


@pytest.mark.api
async def test_open_request_inactive_counter_rejected(
    client: AsyncClient,
    org_a: Organization,
    operator,
    db_session: AsyncSession,
):
    counter = await _make_counter(db_session, org_a, status=CounterStatus.INACTIVE)
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter.id),
    )
    assert res.status_code == 400
    assert "not active" in res.json()["detail"]


@pytest.mark.api
async def test_open_request_unknown_or_foreign_service_rejected(
    client: AsyncClient,
    org_a: Organization,
    org_b: Organization,
    operator,
    counter_a: Counter,
    db_session: AsyncSession,
):
    foreign_service = Service(
        organization_id=org_b.id, name="Foreign", price=Decimal("10.00")
    )
    db_session.add(foreign_service)
    await db_session.commit()

    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json={"counter_id": counter_a.id, "services": [foreign_service.id]},
    )
    # Cross-tenant services are indistinguishable from unknown ones; the
    # global 404 handler normalizes the body across routes.
    assert res.status_code == 404


@pytest.mark.api
async def test_resent_request_updates_pending_row_in_place(
    client: AsyncClient,
    operator,
    counter_a: Counter,
    service_a: Service,
    db_session: AsyncSession,
):
    """Correction flow: resubmitting while pending updates the same row."""
    res1 = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id),
    )
    assert res1.status_code == 200
    shift_id = res1.json()["data"]["id"]

    res2 = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id, service_a.id),
    )
    assert res2.status_code == 200
    assert res2.json()["data"]["id"] == shift_id
    assert res2.json()["data"]["requested_services"] == [service_a.id]

    rows = (
        (await db_session.execute(db_import_counter_shift_filter(operator.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


# ── SHIFT-002: head decision ─────────────────────────────────────────────────


@pytest.mark.api
async def test_head_approval_records_period_services_and_payment_permission(
    client: AsyncClient,
    org_a: Organization,
    operator,
    counter_a: Counter,
    service_a: Service,
    db_session: AsyncSession,
):
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id, service_a.id),
    )
    shift_id = res.json()["data"]["id"]

    decision = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_a),
        json={
            "decision": "approve",
            "reason": "Device verified and cash float confirmed",
            "approved_services": [service_a.id],
            "approved_period_start": "09:00",
            "approved_period_end": "17:30",
            "payment_permitted": True,
        },
    )
    assert decision.status_code == 200, decision.text
    data = decision.json()["data"]
    assert data["status"] == "approved"
    assert data["decided_by_type"] == "organization"
    assert data["payment_permitted"] is True
    assert data["approved_services"] == [service_a.id]
    assert data["approved_period_start"] == "09:00:00"
    assert data["approved_period_end"] == "17:30:00"
    assert "cash float" in data["decided_reason"]


@pytest.mark.api
async def test_rejection_records_reason_and_keeps_pending_for_correction(
    client: AsyncClient,
    org_a: Organization,
    operator,
    counter_a: Counter,
    service_a: Service,
    db_session: AsyncSession,
):
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id),
    )
    shift_id = res.json()["data"]["id"]

    reject = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_a),
        json={"decision": "reject", "reason": "Device not ready; fix printer"},
    )
    assert reject.status_code == 200
    data = reject.json()["data"]
    assert data["status"] == "open_requested"
    assert data["decided_reason"] == "Device not ready; fix printer"
    assert data["approved_services"] is None

    # Employee corrects fields; head approves afterwards.
    corrected = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id, service_a.id),
    )
    assert corrected.status_code == 200
    assert corrected.json()["data"]["id"] == shift_id

    approve = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_a),
        json={"decision": "approve", "reason": "Printer fixed"},
    )
    assert approve.status_code == 200
    assert approve.json()["data"]["status"] == "approved"
    assert approve.json()["data"]["approved_services"] == [service_a.id]


@pytest.mark.api
async def test_decision_requires_pending_status(
    client: AsyncClient,
    org_a: Organization,
    operator,
    counter_a: Counter,
):
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id),
    )
    shift_id = res.json()["data"]["id"]
    await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_a),
        json={"decision": "approve", "reason": "OK"},
    )
    again = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_a),
        json={"decision": "approve", "reason": "Double"},
    )
    assert again.status_code == 409


@pytest.mark.api
async def test_decision_reason_is_required_in_both_directions(
    client: AsyncClient,
    org_a: Organization,
    operator,
    counter_a: Counter,
):
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id),
    )
    shift_id = res.json()["data"]["id"]
    approve = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_a),
        json={"decision": "approve", "reason": "   "},
    )
    assert approve.status_code == 400
    reject = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_a),
        json={"decision": "reject", "reason": "  "},
    )
    assert reject.status_code == 400


@pytest.mark.api
async def test_cross_tenant_decision_is_404_and_list_is_scoped(
    client: AsyncClient,
    org_a: Organization,
    org_b: Organization,
    operator,
    counter_a: Counter,
    db_session: AsyncSession,
):
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id),
    )
    shift_id = res.json()["data"]["id"]

    foreign = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_b),
        json={"decision": "approve", "reason": "Escalation attempt"},
    )
    assert foreign.status_code == 404

    # Tenant isolation for listings as well
    list_b = await client.get("/org/shifts", headers=org_headers(org_b))
    assert list_b.status_code == 200
    assert list_b.json()["data"] == []

    list_a = await client.get("/org/shifts", headers=org_headers(org_a))
    ids = [row["id"] for row in list_a.json()["data"]]
    assert shift_id in ids


@pytest.mark.api
async def test_employee_without_permission_cannot_decide(
    client: AsyncClient,
    org_a: Organization,
    operator,
    counter_a: Counter,
    db_session: AsyncSession,
):
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id),
    )
    shift_id = res.json()["data"]["id"]
    res = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=employee_headers(operator),
        json={"decision": "approve", "reason": "Self approval"},
    )
    assert res.status_code == 403


@pytest.mark.api
async def test_delegated_employee_with_shifts_approve_permission_can_decide(
    client: AsyncClient,
    org_a: Organization,
    operator,
    delegated_manager,
    counter_a: Counter,
):
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id),
    )
    shift_id = res.json()["data"]["id"]
    decision = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=employee_headers(delegated_manager),
        json={"decision": "approve", "reason": "Delegate confirmed device"},
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["data"]["decided_by_type"] == "employee"
    assert decision.json()["data"]["status"] == "approved"


# ── Activation + SHIFT-003 guard primitive ───────────────────────────────────


@pytest.mark.api
async def test_pending_shift_cannot_be_activated(
    client: AsyncClient,
    operator,
    counter_a: Counter,
):
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id),
    )
    shift_id = res.json()["data"]["id"]
    res = await client.post(
        f"/employee/shifts/{shift_id}/activate",
        headers=employee_headers(operator),
    )
    assert res.status_code == 409
    assert "Only an approved shift can be activated" in res.json()["detail"]


@pytest.mark.api
async def test_activation_requires_ownership_by_requesting_employee(
    client: AsyncClient,
    org_a: Organization,
    operator,
    counter_a: Counter,
    db_session: AsyncSession,
):
    """Another employee of the same org cannot activate someone else's shift."""
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id),
    )
    shift_id = res.json()["data"]["id"]
    approve = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_a),
        json={"decision": "approve", "reason": "Approved"},
    )
    assert approve.status_code == 200

    other = await _make_employee(db_session, org_a, f"OP-{uuid.uuid4().hex[:6]}")
    res = await client.post(
        f"/employee/shifts/{shift_id}/activate",
        headers=employee_headers(other),
    )
    assert res.status_code == 403
    assert "Only the requesting employee" in res.json()["detail"]


@pytest.mark.api
async def test_activation_flow_end_to_end_and_guard(
    client: AsyncClient,
    org_a: Organization,
    operator,
    counter_a: Counter,
    service_a: Service,
    db_session: AsyncSession,
):
    """Full path: request -> approval -> activation -> guard sees ACTIVE."""
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id, service_a.id),
    )
    shift_id = res.json()["data"]["id"]
    approve = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_a),
        json={"decision": "approve", "reason": "Approved for morning duty"},
    )
    assert approve.status_code == 200

    activate = await client.post(
        f"/employee/shifts/{shift_id}/activate",
        headers=employee_headers(operator),
    )
    assert activate.status_code == 200, activate.text
    assert activate.json()["data"]["status"] == "active"

    raw = await db_session.execute(db_import_counter_shift_filter(operator.id))
    rows = raw.scalars().all()
    assert len(rows) == 1
    active = rows[0]
    assert CounterShiftStatus(active.status) == CounterShiftStatus.ACTIVE

    guard = await shift_service.get_active_shift(
        db_session, org_a.id, counter_a.id, operator.id
    )
    assert guard is not None and guard.id == shift_id
    ok = await shift_service.require_active_shift(
        db_session, org_a.id, counter_a.id, operator.id
    )
    assert ok.id == shift_id

    # Double activation is a state conflict
    again = await client.post(
        f"/employee/shifts/{shift_id}/activate",
        headers=employee_headers(operator),
    )
    assert again.status_code == 409


@pytest.mark.api
async def test_guard_denies_counter_operations_without_active_shift(
    client: AsyncClient,
    org_a: Organization,
    operator,
    counter_a: Counter,
    db_session: AsyncSession,
):
    """Release Gate 4 primitive: no ACTIVE shift means the guard raises."""
    from models.counter_shift import CounterShiftStatus

    assert await shift_service.get_active_shift(
        db_session, org_a.id, counter_a.id, operator.id
    ) is None
    with pytest.raises(PermissionError):
        await shift_service.require_active_shift(
            db_session, org_a.id, counter_a.id, operator.id
        )

    # A mere approval (without activation) is also not sufficient.
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json=open_payload(counter_a.id),
    )
    shift_id = res.json()["data"]["id"]
    approve = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org_a),
        json={"decision": "approve", "reason": "Approved, not yet clocked in"},
    )
    assert approve.status_code == 200
    with pytest.raises(PermissionError):
        await shift_service.require_active_shift(
            db_session, org_a.id, counter_a.id, operator.id
        )
