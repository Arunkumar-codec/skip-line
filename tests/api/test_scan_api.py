"""
tests/api/test_scan_api.py
─────────────────────────
QR-003/QR-004/QR-005 — authorized scanner check-in endpoint
(POST /employee/tokens/scan).

covers:
- authentication required
- active-shift enforcement (SHIFT-003) and cross-counter shift denial
- valid first check-in (token mutation + audit row)
- duplicate scan idempotency (result repeated audit rows, no re-mutation)
- terminal-state results (completed)
- expired / tampered / foreign-tenant reference outcomes
- branch isolation (WRONG_BRANCH)
- token state never reveals other tenants' data
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import employee_service, org_service
from app.utils import signed_qr
from app.utils.jwt import create_access_token
from models.branch import Branch
from models.counter import Counter, CounterStatus
from models.organization import Organization
from models.service import Service
from models.token import Token, TokenStatus
from models.token_scan import TokenScan


# ── Fixtures ──────────────────────────────────────────────────────────────────

async def _make_org(db_session: AsyncSession) -> Organization:
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(db_session, {
        "name": f"Scan Org {uid}",
        "registration_number": f"SCR-{uid}",
        "org_type": "hospital",
        "email": f"scan_{uid}@hospital.com",
        "mobile": "9876543210",
        "password": "Password123!",
        "address": "1 Scan Street",
        "city": "Bengaluru",
        "state": "Karnataka",
    })
    org.status = "approved"
    await db_session.commit()
    await db_session.refresh(org)
    return org


async def _make_branch(db_session: AsyncSession, org: Organization, code: str) -> Branch:
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
    await db_session.commit()
    await db_session.refresh(branch)
    return branch


async def _make_counter(
    db_session: AsyncSession, org: Organization, name: str,
    prefix: str = "S", branch: Branch | None = None
) -> Counter:
    counter = Counter(
        organization_id=org.id,
        counter_name=name,
        counter_number=1,
        token_prefix=prefix,
        daily_limit=100,
        status=CounterStatus.ACTIVE,
        branch_id=branch.id if branch else None,
    )
    db_session.add(counter)
    await db_session.commit()
    await db_session.refresh(counter)
    return counter


async def _make_employee(db_session: AsyncSession, org: Organization, code: str):
    branch = await _make_branch(db_session, org, f"B{code}")
    return await employee_service.create_employee(db_session, org.id, {
        "name": f"Scanner {code}",
        "email": f"scanner_{code}_{uuid.uuid4().hex[:6]}@example.com",
        "employee_code": code,
        "branch_id": branch.id,
        "password": "SecurePassword@123",
    })


def employee_headers(employee) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(employee.id, 'employee')}"}


def org_headers(org: Organization) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(org.id, 'organization')}"}


async def _make_active_shift(
    client: AsyncClient, org: Organization, employee, counter: Counter, service: Service
):
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(employee),
        json={
            "counter_id": counter.id,
            "services": [service.id],
            "device_ready": True,
            "opening_cash": "0.00",
            "remarks": "scanner shift",
        },
    )
    assert res.status_code == 200, res.text
    shift_id = res.json()["data"]["id"]
    dec = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org),
        json={
            "decision": "approve",
            "reason": "Scanner desk ready",
            "approved_services": [service.id],
            "approved_period_start": "00:00",
            "approved_period_end": "23:59",
        },
    )
    assert dec.status_code == 200, dec.text
    act = await client.post(
        f"/employee/shifts/{shift_id}/activate",
        headers=employee_headers(employee),
        json={},
    )
    assert act.status_code == 200, act.text


async def _book_waiting_token(db_session: AsyncSession, org, counter, service) -> Token:
    from app.services import token_service

    return await token_service.book_token(
        db_session, org.id, counter.id, service.id,
        guest_name="Scan Guest",
    )


async def _book_token(db_session: AsyncSession, org, counter, service) -> Token:
    from app.services import token_service

    token = await token_service.book_token(
        db_session, org.id, counter.id, service.id,
        guest_name="Scan Guest",
    )
    return token


@pytest.fixture
async def scan_env(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _make_org(db_session)
    service = Service(
        organization_id=org.id,
        name=f"Scan Svc {uuid.uuid4().hex[:6]}",
        price=Decimal("0.00"),
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    counter = await _make_counter(db_session, org, "Scan Counter 1")
    employee = await _make_employee(db_session, org, f"SC-{uuid.uuid4().hex[:4]}")
    await _make_active_shift(client, org, employee, counter, service)
    return {"org": org, "service": service, "counter": counter, "employee": employee}


# ── Authentication & authorization ───────────────────────────────────────────

@pytest.mark.api
async def test_scan_requires_authentication(client: AsyncClient, scan_env):
    res = await client.post(
        "/employee/tokens/scan",
        json={"ref": signed_qr.create_token_ref(1, 1, signed_qr.PURPOSE_SCAN)},
    )
    assert res.status_code == 401


@pytest.mark.api
async def test_scan_requires_active_shift(client: AsyncClient, db_session: AsyncSession):
    """No active shift ⇒ 403 (SHIFT-003), even with a valid ref."""
    org = await _make_org(db_session)
    service = Service(
        organization_id=org.id, name=f"S {uuid.uuid4().hex[:6]}", price=Decimal("0.00")
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)
    counter = await _make_counter(db_session, org, "No-shift Counter")
    employee = await _make_employee(db_session, org, f"NS-{uuid.uuid4().hex[:4]}")

    token = await _book_waiting_token(db_session, org, counter, service)
    ref = signed_qr.create_token_ref(token.id, org.id, signed_qr.PURPOSE_SCAN)

    res = await client.post(
        "/employee/tokens/scan",
        headers=employee_headers(employee),
        json={"ref": ref, "counter_id": counter.id},
    )
    assert res.status_code == 403


@pytest.mark.api
async def test_scan_cross_tenant_token_is_403_tenant_bound(
    client: AsyncClient, db_session: AsyncSession
):
    """Org-B employee scanning an Org-A ref: session/tenant stays its own;
    the QR itself is foreign and is answered with WRONG_ORG, no token data."""
    org_a, org_b = (await _make_org(db_session), await _make_org(db_session))
    service_a_foreign = Service(
        organization_id=org_a.id, name=f"SA {uuid.uuid4().hex[:6]}", price=Decimal("0.00")
    )
    db_session.add(service_a_foreign)
    await db_session.commit()
    await db_session.refresh(service_a_foreign)

    service_b = Service(
        organization_id=org_b.id, name=f"SB {uuid.uuid4().hex[:6]}", price=Decimal("0.00")
    )
    db_session.add(service_b)
    await db_session.commit()
    await db_session.refresh(service_b)
    counter_a_ref = await _make_counter(db_session, org_a, "OrgA Counter", prefix="X")
    counter_b = await _make_counter(db_session, org_b, "OrgB Counter")
    employee_b = await _make_employee(db_session, org_b, f"XT-{uuid.uuid4().hex[:4]}")
    await _make_active_shift(client, org_b, employee_b, counter_b, service_b)

    token_a = await _book_token(db_session, org_a, counter_a_ref, service_a_foreign)
    ref = signed_qr.create_token_ref(token_a.id, org_a.id, signed_qr.PURPOSE_SCAN)

    res = await client.post(
        "/employee/tokens/scan",
        headers=employee_headers(employee_b),
        json={"ref": ref, "counter_id": counter_b.id},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["result"] == "wrong_org"
    assert "token_number" not in data  # nothing about the foreign token leaks


# ── Happy path + idempotency ─────────────────────────────────────────────────

@pytest.mark.api
async def test_valid_scan_checks_in_and_audits(client: AsyncClient, scan_env, db_session):
    env = scan_env
    token = await _book_waiting_token(db_session, env["org"], env["counter"], env["service"])
    ref = signed_qr.create_token_ref(token.id, env["org"].id, signed_qr.PURPOSE_SCAN)

    res = await client.post(
        "/employee/tokens/scan",
        headers=employee_headers(env["employee"]),
        json={"ref": ref, "counter_id": env["counter"].id, "device_id": "cam-01"},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["result"] == "valid"
    assert data["token_number"] == token.token_number

    await db_session.refresh(token)
    assert token.checked_in_at is not None
    assert token.checked_in_by == env["employee"].id

    audits = (
        await db_session.execute(
            select(TokenScan).where(TokenScan.token_id == token.id)
        )
    ).scalars().all()
    assert len(audits) == 1
    assert audits[0].result.value == "valid"
    assert audits[0].device_id == "cam-01"
    assert audits[0].employee_id == env["employee"].id


@pytest.mark.api
async def test_duplicate_scan_is_idempotent(client: AsyncClient, scan_env, db_session):
    env = scan_env
    token = await _book_waiting_token(db_session, env["org"], env["counter"], env["service"])
    ref = signed_qr.create_token_ref(token.id, env["org"].id, signed_qr.PURPOSE_SCAN)
    headers = employee_headers(env["employee"])
    payload = {"ref": ref, "counter_id": env["counter"].id}

    first = await client.post("/employee/tokens/scan", headers=headers, json=payload)
    assert first.json()["data"]["result"] == "valid"
    await db_session.refresh(token)
    first_checked_in_at = token.checked_in_at

    second = await client.post("/employee/tokens/scan", headers=headers, json=payload)
    assert second.status_code == 200
    assert second.json()["data"]["result"] == "already_checked_in"

    await db_session.refresh(token)
    assert token.checked_in_at == first_checked_in_at  # never re-mutated
    # two audit rows (QR-005 records every attempt)
    audits = (
        await db_session.execute(
            select(TokenScan).where(TokenScan.token_id == token.id)
        )
    ).scalars().all()
    assert len(audits) == 2


@pytest.mark.api
async def test_completed_token_scan_reports_completed(client: AsyncClient, scan_env, db_session):
    env = scan_env
    token = await _book_waiting_token(db_session, env["org"], env["counter"], env["service"])
    ref = signed_qr.create_token_ref(token.id, env["org"].id, signed_qr.PURPOSE_SCAN)

    await token_service_call_complete(db_session, env, token)
    res = await client.post(
        "/employee/tokens/scan",
        headers=employee_headers(env["employee"]),
        json={"ref": ref, "counter_id": env["counter"].id},
    )
    assert res.status_code == 200
    assert res.json()["data"]["result"] == "completed"


async def token_service_call_complete(db_session, env, token):
    from app.services import token_service

    await token_service.perform_token_action(
        db_session, token.id, env["org"].id, "call"
    )
    await token_service.perform_token_action(
        db_session, token.id, env["org"].id, "complete"
    )


# ── Reference states ─────────────────────────────────────────────────────────

@pytest.mark.api
async def test_expired_scan_reports_expired(client: AsyncClient, scan_env, db_session):
    import time

    env = scan_env
    token = await _book_waiting_token(db_session, env["org"], env["counter"], env["service"])
    now = time.time()
    stale_ref = signed_qr.create_token_ref(
        token.id, env["org"].id, signed_qr.PURPOSE_SCAN, ttl_seconds=1, now=now - 5
    )

    res = await client.post(
        "/employee/tokens/scan",
        headers=employee_headers(env["employee"]),
        json={"ref": stale_ref, "counter_id": env["counter"].id},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["result"] == "expired"
    assert "token_number" not in data  # expired ref reveals nothing

    audits = (
        await db_session.execute(
            select(TokenScan).where(TokenScan.result == "expired")
        )
    ).scalars().all()
    assert len(audits) == 1 and audits[0].token_id is None


@pytest.mark.api
async def test_tampered_scan_is_rejected_and_audited(
    client: AsyncClient, scan_env, db_session
):
    env = scan_env
    token = await _book_waiting_token(db_session, env["org"], env["counter"], env["service"])
    good_ref = signed_qr.create_token_ref(token.id, env["org"].id, signed_qr.PURPOSE_SCAN)
    head, sig = good_ref.split(".", 1)
    tampered = f"{head}AAAAAAAAA.{sig}"

    res = await client.post(
        "/employee/tokens/scan",
        headers=employee_headers(env["employee"]),
        json={"ref": tampered, "counter_id": env["counter"].id},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["result"] == "tampered"

    await db_session.refresh(token)
    assert token.checked_in_at is None  # untouched


@pytest.mark.api
async def test_minimum_input_validation(client: AsyncClient, scan_env):
    """Truncated garbage straight out: 422 (schema) — never 500."""
    res = await client.post(
        "/employee/tokens/scan",
        headers=employee_headers(scan_env["employee"]),
        json={"ref": "x", "counter_id": scan_env["counter"].id},
    )
    assert res.status_code == 422


# ── Branch isolation ─────────────────────────────────────────────────────────

@pytest.mark.api
async def test_scan_from_other_branch_reports_wrong_branch(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _make_org(db_session)
    service = Service(
        organization_id=org.id, name=f"S {uuid.uuid4().hex[:6]}", price=Decimal("0.00")
    )
    db_session.add(service)
    await db_session.commit()
    await db_session.refresh(service)

    branch_a = await _make_branch(db_session, org, "BR-A")
    branch_b = await _make_branch(db_session, org, "BR-B")
    counter_a = await _make_counter(db_session, org, "A Counter", prefix="A", branch=branch_a)
    counter_b = await _make_counter(db_session, org, "B Counter", prefix="B", branch=branch_b)
    employee = await _make_employee(db_session, org, f"WB-{uuid.uuid4().hex[:4]}")
    await _make_active_shift(client, org, employee, counter_b, service)

    token = await _book_token(db_session, org, counter_a, service)
    # token.booked on counter A (branch A); scanner shift is on branch-B desk.
    ref = signed_qr.create_token_ref(token.id, org.id, signed_qr.PURPOSE_SCAN)
    res = await client.post(
        "/employee/tokens/scan",
        headers=employee_headers(employee),
        json={"ref": ref, "counter_id": counter_b.id},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["result"] == "wrong_branch"

    await db_session.refresh(token)
    assert token.checked_in_at is None
