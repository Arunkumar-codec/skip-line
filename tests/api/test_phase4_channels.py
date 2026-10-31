"""
tests/api/test_phase4_channels.py
─────────────────────────────────
Phase 4 — Channels end-to-end integration tests:
- SHIFT-003: Active-shift authorization across employee counter operations.
- SHIFT-004: Shift pause, handover, resumption and formal closure.
- SHIFT-005/006: Cash reconciliation, variance, head review & adjustments.
- PHY-001..004: Assisted physical/walk-in booking, payments, waivers, evidence.
- KSK-001..006: Kiosk device pairing, heartbeat, revocation and idempotency.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import employee_service, org_service, shift_service, token_service, user_service
from app.utils.jwt import create_access_token
from models.branch import Branch
from models.capacity_rule import BookingSource
from models.counter import Counter, CounterStatus
from models.counter_shift import CounterShift, CounterShiftStatus
from models.kiosk_device import KioskDevice
from models.organization import Organization
from models.payment import Payment
from models.service import Service
from models.token import Token, TokenStatus


# ── Helpers & Fixtures ────────────────────────────────────────────────────────


async def _make_org(db: AsyncSession) -> Organization:
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(
        db,
        {
            "name": f"P4 Org {uid}",
            "registration_number": f"P4-{uid}",
            "org_type": "bank",
            "email": f"p4_{uid}@test.org",
            "mobile": "9876543210",
            "password": "Password123!",
            "address": "42 Channel Ave",
            "city": "Bengaluru",
            "state": "Karnataka",
        },
    )
    org.status = "approved"
    await db.commit()
    await db.refresh(org)
    return org


async def _make_counter(db: AsyncSession, org: Organization) -> Counter:
    counter = Counter(
        organization_id=org.id,
        counter_name=f"Counter-{uuid.uuid4().hex[:4]}",
        counter_number=1,
        token_prefix="P4",
        daily_limit=100,
        status=CounterStatus.ACTIVE,
    )
    db.add(counter)
    await db.commit()
    await db.refresh(counter)
    return counter


async def _make_service(db: AsyncSession, org: Organization, price: Decimal = Decimal("50.00")) -> Service:
    service = Service(
        organization_id=org.id,
        name=f"Service-{uuid.uuid4().hex[:4]}",
        price=price,
        is_active=True,
    )
    db.add(service)
    await db.commit()
    await db.refresh(service)
    return service


async def _make_employee(db: AsyncSession, org: Organization, code: str):
    branch = Branch(
        organization_id=org.id,
        name=f"Branch {code}",
        branch_code=code,
        address="Ground Floor",
        city="Bengaluru",
        state="Karnataka",
        pincode="560001",
    )
    db.add(branch)
    await db.flush()
    employee = await employee_service.create_employee(
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
    return employee


def employee_headers(employee) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(employee.id, 'employee')}"}


def org_headers(org: Organization) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(org.id, 'organization')}"}


async def _create_active_shift(client: AsyncClient, org: Organization, operator, counter: Counter, service: Service) -> int:
    """Helper to request, approve, and activate a shift with payment permitted."""
    res = await client.post(
        "/employee/shifts/open-request",
        headers=employee_headers(operator),
        json={
            "counter_id": counter.id,
            "services": [service.id],
            "device_ready": True,
            "opening_cash": "200.00",
            "remarks": "Active shift setup",
        },
    )
    assert res.status_code == 200, res.text
    shift_id = res.json()["data"]["id"]

    approve = await client.post(
        f"/org/shifts/{shift_id}/decision",
        headers=org_headers(org),
        json={
            "decision": "approve",
            "reason": "Authorized shift",
            "payment_permitted": True,
        },
    )
    assert approve.status_code == 200, approve.text

    activate = await client.post(
        f"/employee/shifts/{shift_id}/activate",
        headers=employee_headers(operator),
    )
    assert activate.status_code == 200, activate.text
    return shift_id


# ── SHIFT-003: Active Shift Authorization Gate ───────────────────────────────


@pytest.mark.api
async def test_employee_queue_operations_blocked_without_active_shift(
    client: AsyncClient,
    db_session: AsyncSession,
):
    org = await _make_org(db_session)
    counter = await _make_counter(db_session, org)
    service = await _make_service(db_session, org)
    operator = await _make_employee(db_session, org, f"OP-{uuid.uuid4().hex[:6]}")

    # Create a waiting token directly
    token = await token_service.book_token(
        db_session,
        org_id=org.id,
        counter_id=counter.id,
        service_id=service.id,
        guest_name="Walk-in Guest",
        guest_mobile="9900000001",
        booking_source=BookingSource.ONLINE,
    )

    # 1. Calling next without an active shift must return 403
    call_next_res = await client.post(
        f"/employee/counters/{counter.id}/call-next",
        headers=employee_headers(operator),
    )
    assert call_next_res.status_code == 403
    assert "approved, active shift is required" in call_next_res.json()["detail"]

    # 2. Token action without active shift must return 403
    action_res = await client.post(
        f"/employee/tokens/{token.id}/action",
        headers=employee_headers(operator),
        json={"action": "call"},
    )
    assert action_res.status_code == 403

    # 3. Walk-in booking without active shift must return 403
    walkin_res = await client.post(
        "/employee/tokens/book-walkin",
        headers=employee_headers(operator),
        json={
            "counter_id": counter.id,
            "service_id": service.id,
            "customer_name": "Walkin Person",
            "customer_phone": "9900000002",
            "payment_method": "cash",
        },
    )
    assert walkin_res.status_code == 403


# ── SHIFT-004: Pause, Resume, Handover ────────────────────────────────────────


@pytest.mark.api
async def test_shift_pause_resume_and_handover(
    client: AsyncClient,
    db_session: AsyncSession,
):
    org = await _make_org(db_session)
    counter = await _make_counter(db_session, org)
    service = await _make_service(db_session, org)
    operator = await _make_employee(db_session, org, f"OP-{uuid.uuid4().hex[:6]}")
    target_op = await _make_employee(db_session, org, f"TG-{uuid.uuid4().hex[:6]}")

    shift_id = await _create_active_shift(client, org, operator, counter, service)

    # Operator pauses shift
    pause_res = await client.post(
        f"/employee/shifts/{shift_id}/pause",
        headers=employee_headers(operator),
        json={"reason": "Lunch break"},
    )
    assert pause_res.status_code == 200
    assert pause_res.json()["data"]["status"] == "paused"

    # Verify counter status set to BREAK
    await db_session.refresh(counter)
    assert counter.status == CounterStatus.BREAK

    # Handover shift to target employee
    handover_res = await client.post(
        f"/employee/shifts/{shift_id}/handover",
        headers=employee_headers(operator),
        json={"target_employee_id": target_op.id, "notes": "Handing over drawer"},
    )
    assert handover_res.status_code == 200
    assert handover_res.json()["data"]["handover_to_employee_id"] == target_op.id

    # Resume shift
    resume_res = await client.post(
        f"/employee/shifts/{shift_id}/resume",
        headers=employee_headers(operator),
    )
    assert resume_res.status_code == 200
    assert resume_res.json()["data"]["status"] == "active"

    # Counter status restored to ACTIVE
    await db_session.refresh(counter)
    assert counter.status == CounterStatus.ACTIVE


# ── SHIFT-005 & 006: Closing Summary, Reconciliation & Adjustment ─────────────


@pytest.mark.api
async def test_shift_closing_reconciliation_and_adjustment(
    client: AsyncClient,
    db_session: AsyncSession,
):
    org = await _make_org(db_session)
    counter = await _make_counter(db_session, org)
    service = await _make_service(db_session, org, price=Decimal("150.00"))
    operator = await _make_employee(db_session, org, f"OP-{uuid.uuid4().hex[:6]}")

    shift_id = await _create_active_shift(client, org, operator, counter, service)

    # Book walk-in cash token to generate cash revenue
    book_res = await client.post(
        "/employee/tokens/book-walkin",
        headers=employee_headers(operator),
        json={
            "counter_id": counter.id,
            "service_id": service.id,
            "customer_name": "Cash Payer",
            "customer_phone": "9800000001",
            "payment_method": "cash",
        },
    )
    assert book_res.status_code == 200

    # Get closing summary: opening 200.00 + cash 150.00 = expected 350.00
    summary_res = await client.get(
        f"/employee/shifts/{shift_id}/closing-summary",
        headers=employee_headers(operator),
    )
    assert summary_res.status_code == 200
    sum_data = summary_res.json()["data"]
    assert Decimal(str(sum_data["opening_cash"])) == Decimal("200.00")
    assert Decimal(str(sum_data["total_cash_collected"])) == Decimal("150.00")
    assert Decimal(str(sum_data["expected_cash"])) == Decimal("350.00")

    # Operator closes shift counting 340.00 (10.00 shortage)
    close_res = await client.post(
        f"/employee/shifts/{shift_id}/close-request",
        headers=employee_headers(operator),
        json={"counted_cash": "340.00", "remarks": "Minor discrepancy"},
    )
    assert close_res.status_code == 200
    c_data = close_res.json()["data"]
    assert c_data["status"] == "close_requested"
    assert Decimal(str(c_data["closing_cash_variance"])) == Decimal("-10.00")

    # Head reconciles shift: record discrepancy
    recon_res = await client.post(
        f"/org/shifts/{shift_id}/reconcile",
        headers=org_headers(org),
        json={"action": "record_discrepancy", "notes": "Approved with shortage note"},
    )
    assert recon_res.status_code == 200
    r_data = recon_res.json()["data"]
    assert r_data["status"] == "closed"
    assert r_data["reconciliation_status"] == "shortage_recorded"

    # Head authorized adjustment: +10.00 found in drawer
    adj_res = await client.post(
        f"/org/shifts/{shift_id}/adjust",
        headers=org_headers(org),
        json={"adjustment_amount": "10.00", "reason": "Found misplaced bill in till"},
    )
    assert adj_res.status_code == 200
    a_data = adj_res.json()["data"]
    assert a_data["reconciliation_status"] == "adjusted"
    assert Decimal(str(a_data["adjustment_amount"])) == Decimal("10.00")


# ── PHY-001..004: Assisted Walk-in Booking & Waivers ──────────────────────────


@pytest.mark.api
async def test_assisted_walkin_booking_and_waiver_workflow(
    client: AsyncClient,
    db_session: AsyncSession,
):
    org = await _make_org(db_session)
    counter = await _make_counter(db_session, org)
    service = await _make_service(db_session, org, price=Decimal("80.00"))
    operator = await _make_employee(db_session, org, f"OP-{uuid.uuid4().hex[:6]}")

    # Register an existing user to test search
    await user_service.register_user(
        db_session,
        {
            "name": "Jane Existing",
            "email": f"jane_{uuid.uuid4().hex[:6]}@example.com",
            "mobile": "9876500001",
            "password": "Password123!",
            "confirm_password": "Password123!",
        },
    )

    shift_id = await _create_active_shift(client, org, operator, counter, service)

    # 1. Search customer by mobile (PHY-001)
    search_res = await client.get(
        "/employee/customers/search?phone=9876500001",
        headers=employee_headers(operator),
    )
    assert search_res.status_code == 200
    users = search_res.json()["data"]
    assert len(users) == 1
    assert users[0]["name"] == "Jane Existing"

    # 2. Assisted booking with waiver requires a reason (PHY-003)
    no_reason_waiver = await client.post(
        "/employee/tokens/book-walkin",
        headers=employee_headers(operator),
        json={
            "counter_id": counter.id,
            "service_id": service.id,
            "customer_name": "Jane Existing",
            "customer_phone": "9876500001",
            "payment_method": "waived",
        },
    )
    assert no_reason_waiver.status_code == 400
    assert "waiver reason is required" in no_reason_waiver.json()["detail"].lower()

    # 3. Successful waiver booking with reason
    waiver_res = await client.post(
        "/employee/tokens/book-walkin",
        headers=employee_headers(operator),
        json={
            "counter_id": counter.id,
            "service_id": service.id,
            "customer_name": "Jane Existing",
            "customer_phone": "9876500001",
            "payment_method": "waived",
            "waiver_reason": "Senior citizen exemption",
        },
    )
    assert waiver_res.status_code == 200
    w_data = waiver_res.json()["data"]
    assert w_data["payment_status"] == "waived"
    assert w_data["payment_method"] == "waived"


# ── KSK-001..006: Kiosk Device Pairing, Heartbeat, Revocation & Idempotency ──


@pytest.mark.api
async def test_kiosk_device_lifecycle_and_idempotency(
    client: AsyncClient,
    db_session: AsyncSession,
):
    org = await _make_org(db_session)
    counter = await _make_counter(db_session, org)
    service = await _make_service(db_session, org)

    device_code = f"kiosk_{uuid.uuid4().hex[:8]}"

    # 1. Device requests pairing PIN (KSK-001 / Open Decision 7)
    pair_req = await client.post(
        "/public/kiosk/pair-request",
        json={"device_code": device_code, "device_name": "Main Entrance Kiosk"},
    )
    assert pair_req.status_code == 200
    pin = pair_req.json()["data"]["pairing_code"]
    assert len(pin) == 6 and pin.isdigit()

    # 2. Org Head confirms pairing PIN in dashboard
    confirm_res = await client.post(
        "/org/kiosks/pair-confirm",
        headers=org_headers(org),
        json={"pairing_code": pin, "device_name": "Main Lobby Kiosk"},
    )
    assert confirm_res.status_code == 200
    kiosk_id = confirm_res.json()["data"]["id"]

    # 3. Device exchanges PIN for API key
    exchange_res = await client.post(
        "/public/kiosk/pair-exchange",
        json={"device_code": device_code, "pairing_code": pin},
    )
    assert exchange_res.status_code == 200
    api_key = exchange_res.json()["data"]["api_key"]
    assert api_key.startswith("kiosk_")

    # 4. Device sends heartbeat (KSK-006)
    heartbeat_res = await client.post(
        "/public/kiosk/heartbeat",
        json={"device_code": device_code},
    )
    assert heartbeat_res.status_code == 200
    assert heartbeat_res.json()["data"]["device_code"] == device_code

    # 5. Kiosk booking with client_reference idempotency (KSK-005)
    idem_ref = f"idem_{uuid.uuid4().hex}"
    book_1 = await client.post(
        "/public/kiosk/book",
        json={
            "organization_id": org.id,
            "counter_id": counter.id,
            "service_id": service.id,
            "guest_name": "Kiosk Visitor",
            "guest_mobile": "9111111111",
            "client_reference": idem_ref,
        },
    )
    assert book_1.status_code == 200
    token_1_id = book_1.json()["data"]["id"]

    # Retry booking with same idempotency reference (simulating printer retry)
    book_2 = await client.post(
        "/public/kiosk/book",
        json={
            "organization_id": org.id,
            "counter_id": counter.id,
            "service_id": service.id,
            "guest_name": "Kiosk Visitor",
            "guest_mobile": "9111111111",
            "client_reference": idem_ref,
        },
    )
    assert book_2.status_code == 200
    token_2_id = book_2.json()["data"]["id"]
    assert token_1_id == token_2_id  # Same token returned, no duplicate!

    # 6. Org Head revokes kiosk device
    revoke_res = await client.post(
        f"/org/kiosks/{kiosk_id}/revoke",
        headers=org_headers(org),
    )
    assert revoke_res.status_code == 200
    assert revoke_res.json()["data"]["is_active"] is False

    # Heartbeat after revocation fails
    bad_hb = await client.post(
        "/public/kiosk/heartbeat",
        json={"device_code": device_code},
    )
    assert bad_hb.status_code == 404

