"""
tests/integration/test_wait_time_tracking.py
────────────────────────────────────────────
Phase 10 (MASTER §16.4–§16.7, §21, §24.7) integration tests over the real
API surface (in-memory SQLite, no external services):

- Live tracking carries the explainable estimate + estimated_at timestamp
  and refreshes after material queue progression (§16.4, §24.7).
- Booking persists the initial prediction snapshot (§16.6) and queue
  events persist recalc snapshots.
- The public display surfaces the estimate + timestamp with ZERO customer
  PII (§16.7 / §24.7).
- The PDF receipt is a fresh, static snapshot per generation; previously
  downloaded bytes never change (§16.5/§22).
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.admin_service import seed_default_admin
from models.wait_time_estimate import WaitTimeEstimate


async def _seed_queue(client: AsyncClient, db_session: AsyncSession,
                      estimated_minutes: int = 10):
    """org + approved + service + counter; returns (org_id, counter_id, service_id)."""
    await seed_default_admin(db_session)
    stamp = settings.admin_email  # unique orgs per call via email
    import time
    uid = str(time.time_ns())[-9:]
    org_resp = await client.post("/org/register", json={
        "name": f"WTE Integration Org {uid}",
        "registration_number": f"WTEI-{uid}",
        "org_type": "hospital",
        "address": "1 Test Road",
        "city": "Testville",
        "state": "Testland",
        "email": f"wtei_{uid}@example.com",
        "mobile": "9000000000",
        "password": "OrgPass@123",
        "confirm_password": "OrgPass@123",
    })
    assert org_resp.status_code == 200, org_resp.text
    org_id = org_resp.json()["data"]["id"]

    admin_login = await client.post("/auth/admin/login", json={
        "email": settings.admin_email,
        "password": settings.admin_password,
    })
    assert admin_login.status_code == 200, admin_login.text
    admin_token = admin_login.json()["access_token"]
    approve = await client.post(
        f"/admin/organizations/{org_id}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert approve.status_code == 200, approve.text

    org_login = await client.post("/auth/org/login", json={
        "email": f"wtei_{uid}@example.com",
        "password": "OrgPass@123",
    })
    assert org_login.status_code == 200, org_login.text
    org_token = org_login.json()["access_token"]
    org_headers = {"Authorization": f"Bearer {org_token}"}

    svc_resp = await client.post("/org/services", json={
        "name": f"Consultation {uid}",
        "price": 0,
        "estimated_minutes": estimated_minutes,
    }, headers=org_headers)
    assert svc_resp.status_code == 200, svc_resp.text
    service_id = svc_resp.json()["data"]["id"]

    ctr_resp = await client.post("/org/counters", json={
        "counter_name": "OPD Counter 1",
        "counter_number": 1,
        "token_prefix": "A",
        "daily_limit": 200,
        "service_id": service_id,
    }, headers=org_headers)
    assert ctr_resp.status_code == 200, ctr_resp.text
    counter_id = ctr_resp.json()["data"]["id"]

    return org_id, counter_id, service_id, org_headers


async def _register_and_login_user(client: AsyncClient, uid: str):
    reg = await client.post("/user/register", json={
        "name": f"Wait Time User {uid}",
        "email": f"wteuser_{uid}@example.com",
        "mobile": f"9000000{uid[-3:]}",
        "password": "UserPass@123",
        "confirm_password": "UserPass@123",
    })
    assert reg.status_code == 200, reg.text
    login = await client.post("/auth/user/login", json={
        "email": f"wteuser_{uid}@example.com",
        "password": "UserPass@123",
    })
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
async def test_tracking_estimate_timestamp_and_live_refresh(
    client: AsyncClient, db_session: AsyncSession
):
    """§16.4/§24.7: estimate + timestamp on tracking; refresh after the
    queue materially changes."""
    org_id, counter_id, service_id, org_headers = await _seed_queue(client, db_session)
    user_headers = await _register_and_login_user(client, "111")

    book1 = await client.post("/user/tokens/book", json={
        "organization_id": org_id, "counter_id": counter_id,
        "service_id": service_id,
    }, headers=user_headers)
    assert book1.status_code == 200, book1.text
    token1 = book1.json()["data"]["id"]

    book2 = await client.post("/user/tokens/book", json={
        "organization_id": org_id, "counter_id": counter_id,
        "service_id": service_id,
    }, headers=user_headers)
    assert book2.status_code == 200, book2.text
    token2 = book2.json()["data"]["id"]

    # token2: one person ahead → estimate = 1 × 10, timestamped, explained
    t2_before = (await client.get(f"/user/tokens/{token2}/track")).json()["data"]
    assert t2_before["people_ahead"] == 1
    assert t2_before["estimated_wait_minutes"] == 10
    assert t2_before["estimated_wait_status"] == "estimate"
    assert t2_before["estimated_at"]
    assert "1 token ahead" in t2_before["estimate_explanation"]
    assert t2_before["estimated_wait_range_min"] <= 10 <= t2_before["estimated_wait_range_max"]

    # token1: nobody ahead → honest 0-minute wait, still timestamped
    t1 = (await client.get(f"/user/tokens/{token1}/track")).json()["data"]
    assert t1["people_ahead"] == 0
    assert t1["estimated_wait_minutes"] == 0

    # Material queue change: call token1 → token2's estimate refreshes live
    call = await client.post(f"/org/tokens/{token1}/action",
                             json={"action": "call"}, headers=org_headers)
    assert call.status_code == 200, call.text

    t2_after = (await client.get(f"/user/tokens/{token2}/track")).json()["data"]
    assert t2_after["people_ahead"] == 0
    assert t2_after["estimated_wait_minutes"] == 0
    assert t2_after["estimated_at"] != t2_before["estimated_at"], (
        "estimate timestamp must refresh when the queue changes (§16.4)"
    )


@pytest.mark.integration
async def test_booking_persists_initial_snapshot_and_recalcs(
    client: AsyncClient, db_session: AsyncSession
):
    """§16.6: booking persists the initial prediction; queue events persist
    recalcs — the accuracy-tracking anchor set."""
    org_id, counter_id, service_id, org_headers = await _seed_queue(client, db_session)
    user_headers = await _register_and_login_user(client, "222")

    book1 = await client.post("/user/tokens/book", json={
        "organization_id": org_id, "counter_id": counter_id,
        "service_id": service_id,
    }, headers=user_headers)
    token1 = book1.json()["data"]["id"]
    book2 = await client.post("/user/tokens/book", json={
        "organization_id": org_id, "counter_id": counter_id,
        "service_id": service_id,
    }, headers=user_headers)
    token2 = book2.json()["data"]["id"]

    initial2 = (await db_session.execute(
        select(WaitTimeEstimate).where(
            WaitTimeEstimate.token_id == token2,
            WaitTimeEstimate.snapshot_kind == "initial",
        )
    )).scalars().all()
    assert len(initial2) == 1, "booking must persist exactly one initial snapshot"
    assert initial2[0].predicted_wait_minutes == 10  # 1 ahead × 10 min
    assert initial2[0].estimation_status == "estimate"

    # Material queue event → recalc snapshot for the next waiting token
    await client.post(f"/org/tokens/{token1}/action",
                      json={"action": "call"}, headers=org_headers)
    recalcs = (await db_session.execute(
        select(WaitTimeEstimate).where(
            WaitTimeEstimate.token_id == token2,
            WaitTimeEstimate.snapshot_kind == "recalc",
        )
    )).scalars().all()
    assert len(recalcs) >= 1, "queue event must persist a recalc snapshot"
    assert recalcs[-1].predicted_wait_minutes == 0  # now next in line


@pytest.mark.integration
async def test_public_display_estimate_without_pii(
    client: AsyncClient, db_session: AsyncSession
):
    """§16.7/§24.7: display shows estimate + timestamp; zero customer PII."""
    org_id, counter_id, service_id, _ = await _seed_queue(client, db_session)

    guest_name = "Privacy Sensitive Person"
    guest_mobile = "9812345678"
    book = await client.post("/public/kiosk/book", json={
        "organization_id": org_id,
        "counter_id": counter_id,
        "service_id": service_id,
        "guest_name": guest_name,
        "guest_mobile": guest_mobile,
    })
    assert book.status_code == 200, book.text

    display = await client.get(f"/public/display/{org_id}")
    assert display.status_code == 200, display.text
    body = display.text

    assert guest_name not in body, "display must never leak guest names (§16.7)"
    assert guest_mobile not in body, "display must never leak guest mobiles (§16.7)"

    data = display.json()["data"]
    counter_data = next(c for c in data["counters"] if c["counter_id"] == counter_id)
    assert counter_data["upcoming_tokens"], "guest token must be upcoming"

    estimate = counter_data["estimate"]
    assert estimate["estimated_wait_minutes"] == 10
    assert estimate["estimated_at"]
    assert estimate["estimated_wait_status"] == "estimate"


@pytest.mark.integration
async def test_pdf_snapshot_timestamp_and_immutability(
    client: AsyncClient, db_session: AsyncSession
):
    """§16.5/§22: each receipt generation embeds the estimate as a static
    snapshot; previously downloaded bytes never change."""
    org_id, counter_id, service_id, org_headers = await _seed_queue(client, db_session)

    book = await client.post("/public/kiosk/book", json={
        "organization_id": org_id,
        "counter_id": counter_id,
        "service_id": service_id,
        "guest_name": "Snapshot Guest",
        "guest_mobile": "9876500000",
    })
    token = book.json()["data"]
    receipt_ref = token["receipt_ref"]
    assert receipt_ref

    receipt1 = await client.get(f"/tokens/{token['id']}/receipt?ref={receipt_ref}")
    assert receipt1.status_code == 200, receipt1.text
    assert len(receipt1.content) > 1000
    pdf1 = receipt1.content

    # Advance the queue (call the token) → a NEW generation is a NEW snapshot
    await client.post(f"/org/tokens/{token['id']}/action",
                      json={"action": "call"}, headers=org_headers)
    receipt2 = await client.get(f"/tokens/{token['id']}/receipt?ref={receipt_ref}")
    assert receipt2.status_code == 200

    # The previously downloaded bytes are immutable; a re-generation is a
    # fresh, distinct snapshot (token now called → no forward-wait rows).
    assert pdf1 != receipt2.content
