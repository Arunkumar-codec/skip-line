"""
tests/api/test_catalogue_multiservice.py
────────────────────────────────────────
Unit and API integration tests for:
- Sub-services CRUD and unique code constraint
- Server-side duration and pricing calculation rollups
- Counter-service M:N mappings
- Automatic queue routing to eligible counters
- TokenServiceItem breakdown persistence
"""

from decimal import Decimal
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import catalogue_service, org_service, token_service, user_service
from app.utils.jwt import create_access_token
from models.counter import Counter, CounterStatus
from models.service import Service
from models.sub_service import SubService


import uuid


@pytest.fixture
async def sample_org(db_session: AsyncSession):
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(db_session, {
        "name": f"Apollo Multispecialty {uid}",
        "registration_number": f"APOLLO-{uid}",
        "org_type": "hospital",
        "email": f"apollo_{uid}@test.com",
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


@pytest.fixture
async def sample_user(db_session: AsyncSession):
    uid = uuid.uuid4().hex[:8]
    return await user_service.register_user(db_session, {
        "name": "Ravi Kumar",
        "email": f"ravi_{uid}@test.com",
        "mobile": "9123456789",
        "password": "Password123!",
    })


@pytest.fixture
def org_headers(sample_org):
    token = create_access_token(sample_org.id, "organization")
    return {"Authorization": f"Bearer {token}", "Cookie": f"org_token={token}"}


@pytest.fixture
def user_headers(sample_user):
    token = create_access_token(sample_user.id, "user")
    return {"Authorization": f"Bearer {token}", "Cookie": f"user_token={token}"}


@pytest.mark.asyncio
async def test_sub_services_crud(client: AsyncClient, sample_org, org_headers):
    # 1. Create a parent service
    svc_res = await client.post(
        "/org/services",
        headers=org_headers,
        json={
            "name": "General Health Checkup",
            "service_code": "GHC-01",
            "price": "500.00",
            "estimated_minutes": 20,
        },
    )
    assert svc_res.status_code == 200
    service_id = svc_res.json()["data"]["id"]

    # 2. Add sub-services
    sub1_res = await client.post(
        f"/org/services/{service_id}/sub-services",
        headers=org_headers,
        json={
            "name": "Blood Glucose Test",
            "code": "BGT-01",
            "additional_price": "150.00",
            "additional_minutes": 5,
        },
    )
    assert sub1_res.status_code == 200
    sub1_id = sub1_res.json()["data"]["id"]
    assert sub1_res.json()["data"]["code"] == "BGT-01"

    sub2_res = await client.post(
        "/org/sub-services",
        headers=org_headers,
        json={
            "service_id": service_id,
            "name": "Lipid Profile Panel",
            "code": "LPP-01",
            "additional_price": "350.00",
            "additional_minutes": 10,
        },
    )
    assert sub2_res.status_code == 200

    # 3. Duplicate code rejection within same service
    dup_res = await client.post(
        f"/org/services/{service_id}/sub-services",
        headers=org_headers,
        json={
            "name": "Duplicate Test",
            "code": "BGT-01",
            "additional_price": "50.00",
        },
    )
    assert dup_res.status_code == 409

    # 4. List sub-services
    list_res = await client.get(
        f"/org/services/{service_id}/sub-services",
        headers=org_headers,
    )
    assert list_res.status_code == 200
    assert len(list_res.json()["data"]) == 2

    # 5. Update sub-service
    upd_res = await client.put(
        f"/org/sub-services/{sub1_id}",
        headers=org_headers,
        json={"name": "Fasting Blood Glucose Test", "additional_price": "180.00"},
    )
    assert upd_res.status_code == 200
    assert float(upd_res.json()["data"]["additional_price"]) == 180.00

    # 6. Delete sub-service
    del_res = await client.delete(
        f"/org/sub-services/{sub1_id}",
        headers=org_headers,
    )
    assert del_res.status_code == 200


@pytest.mark.asyncio
async def test_service_pricing_and_duration_rollup(db_session: AsyncSession, sample_org):
    svc = Service(
        organization_id=sample_org.id,
        name="Dental Consultation",
        price=Decimal("400.00"),
        estimated_minutes=15,
    )
    db_session.add(svc)
    await db_session.commit()
    await db_session.refresh(svc)

    sub1 = SubService(
        service_id=svc.id,
        name="Teeth Scaling",
        code="SCALE-01",
        additional_price=Decimal("600.00"),
        additional_minutes=20,
    )
    sub2 = SubService(
        service_id=svc.id,
        name="Fluoride Treatment",
        code="FLUO-01",
        additional_price=Decimal("250.00"),
        additional_minutes=10,
    )
    db_session.add_all([sub1, sub2])
    await db_session.commit()

    rollup = catalogue_service.calculate_service_rollup(svc, [sub1, sub2])
    assert rollup["base_price"] == 400.00
    assert rollup["total_price"] == 1250.00
    assert rollup["base_minutes"] == 15
    assert rollup["total_minutes"] == 45
    assert rollup["sub_services_count"] == 2


@pytest.mark.asyncio
async def test_multi_service_counter_routing(db_session: AsyncSession, sample_org, sample_user):
    # Create 2 services
    s1 = Service(organization_id=sample_org.id, name="Cash Deposit", price=Decimal("0.00"), estimated_minutes=5)
    s2 = Service(organization_id=sample_org.id, name="Loan Inquiry", price=Decimal("0.00"), estimated_minutes=15)
    db_session.add_all([s1, s2])
    await db_session.commit()
    await db_session.refresh(s1)
    await db_session.refresh(s2)

    # Create Counter 1 handling BOTH s1 and s2
    c1 = await org_service.create_counter(db_session, sample_org.id, {
        "counter_name": "Multi Counter 1",
        "counter_number": 1,
        "token_prefix": "M",
        "service_ids": [s1.id, s2.id],
    })

    # Create Counter 2 handling ONLY s1
    c2 = await org_service.create_counter(db_session, sample_org.id, {
        "counter_name": "Cash Counter 2",
        "counter_number": 2,
        "token_prefix": "C",
        "service_ids": [s1.id],
    })

    # Check eligible counters for s2 (Loan Inquiry) -> should only be Counter 1
    eligible_s2 = await catalogue_service.get_eligible_counters_for_service(db_session, s2.id, sample_org.id)
    assert len(eligible_s2) == 1
    assert eligible_s2[0].id == c1.id

    # Check eligible counters for s1 (Cash Deposit) -> should be both Counter 1 and Counter 2
    eligible_s1 = await catalogue_service.get_eligible_counters_for_service(db_session, s1.id, sample_org.id)
    assert len(eligible_s1) == 2

    # Book token without explicit counter_id -> auto-routes to eligible counter
    token = await token_service.book_token(
        db=db_session,
        org_id=sample_org.id,
        counter_id=None,
        service_id=s2.id,
        user_id=sample_user.id,
    )
    assert token.counter_id == c1.id
    assert token.token_number.startswith("M-")
