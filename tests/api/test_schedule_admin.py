"""
tests/api/test_schedule_admin.py
────────────────────────────────
API-level tests for the Phase 3 schedule-management endpoints:
authorized CRUD lifecycle, authorization (401), validation (400/422),
duplicate conflicts (400), cross-tenant isolation (404) and audit-log
behavior for schedule mutations.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.admin_service import seed_default_admin

_seq = 0


def _next_email() -> str:
    global _seq
    _seq += 1
    return f"schedapi{_seq}@example.com"


def _next_reg() -> str:
    global _seq
    _seq += 1
    return f"SA-API-{_seq:05d}"


async def _register_and_approve_org(
    client: AsyncClient, db_session: AsyncSession
) -> str:
    """Seed admin, register + approve an org, return its access token."""
    await seed_default_admin(db_session)
    email, reg = _next_email(), _next_reg()

    resp = await client.post(
        "/org/register",
        json={
            "name": f"Schedule API Org {_seq}",
            "registration_number": reg,
            "org_type": "hospital",
            "address": "1 Schedule Ave",
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

    admin_login = await client.post(
        "/auth/admin/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert admin_login.status_code == 200
    admin_token = admin_login.json()["access_token"]
    approve = await client.post(
        f"/admin/organizations/{org_id}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert approve.status_code == 200

    org_login = await client.post(
        "/auth/org/login", json={"email": email, "password": "Password@123"}
    )
    assert org_login.status_code == 200
    return org_login.json()["access_token"]


WEEKLY = {
    "scope_level": "org",
    "day_of_week": 0,
    "open_time": "09:00:00",
    "close_time": "17:00:00",
    "slot_interval_minutes": 30,
}
# scope_id defaults to the caller's own org id — resolved per test


@pytest.mark.api
async def test_weekly_schedule_lifecycle(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_approve_org(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}

    # Discover own org id from profile
    profile = await client.get("/org/api/profile", headers=headers)
    org_id = profile.json()["data"]["id"]
    payload = {**WEEKLY, "scope_id": org_id}

    create = await client.post("/org/schedules/weekly", json=payload, headers=headers)
    assert create.status_code == 200, create.text
    row = create.json()["data"]
    assert row["day_of_week"] == 0
    assert row["scope_level"] == "org"
    schedule_id = row["id"]

    # List
    listing = await client.get("/org/api/schedules/weekly", headers=headers)
    assert listing.status_code == 200
    assert [r["id"] for r in listing.json()["data"]] == [schedule_id]

    # Get single
    got = await client.get(f"/org/schedules/weekly/{schedule_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["data"]["open_time"].startswith("09:00")

    # Update
    upd = await client.put(
        f"/org/schedules/weekly/{schedule_id}",
        json={"open_time": "08:30:00"},
        headers=headers,
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["open_time"].startswith("08:30")

    # Delete
    dele = await client.delete(f"/org/schedules/weekly/{schedule_id}", headers=headers)
    assert dele.status_code == 200
    gone = await client.get(f"/org/schedules/weekly/{schedule_id}", headers=headers)
    assert gone.status_code == 404


@pytest.mark.api
async def test_unauthenticated_schedule_access_rejected(client: AsyncClient):
    assert (await client.get("/org/api/schedules/weekly")).status_code == 401
    assert (
        await client.post("/org/schedules/weekly", json={})
    ).status_code == 401


@pytest.mark.api
async def test_weekly_validation_errors(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_approve_org(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}
    org_id = (await client.get("/org/api/profile", headers=headers)).json()["data"]["id"]

    # Reversed hours → 400
    resp = await client.post("/org/schedules/weekly", json={
        "scope_level": "org", "scope_id": org_id, "day_of_week": 1,
        "open_time": "17:00:00", "close_time": "09:00:00",
    }, headers=headers)
    assert resp.status_code == 400
    assert "earlier" in resp.json()["detail"]

    # Missing hours → 400
    resp = await client.post("/org/schedules/weekly", json={
        "scope_level": "org", "scope_id": org_id, "day_of_week": 2,
    }, headers=headers)
    assert resp.status_code == 400

    # day_of_week out of range → 422 (schema bound)
    resp = await client.post("/org/schedules/weekly", json={
        "scope_level": "org", "scope_id": org_id, "day_of_week": 7,
        "open_time": "09:00:00", "close_time": "17:00:00",
    }, headers=headers)
    assert resp.status_code == 422

    # Duplicate → 400 with safe message
    good = {
        "scope_level": "org", "scope_id": org_id, "day_of_week": 3,
        "open_time": "09:00:00", "close_time": "17:00:00",
    }
    first = await client.post("/org/schedules/weekly", json=good, headers=headers)
    assert first.status_code == 200
    dup = await client.post("/org/schedules/weekly", json=good, headers=headers)
    assert dup.status_code == 400
    assert "already exists" in dup.json()["detail"]
    assert "sqlite" not in dup.json()["detail"].lower()


@pytest.mark.api
async def test_cross_tenant_schedule_access_blocked(
    client: AsyncClient, db_session: AsyncSession
):
    org_a_token = await _register_and_approve_org(client, db_session)
    org_b_token = await _register_and_approve_org(client, db_session)
    a_headers = {"Authorization": f"Bearer {org_a_token}"}
    b_headers = {"Authorization": f"Bearer {org_b_token}"}
    a_org_id = (await client.get("/org/api/profile", headers=a_headers)).json()["data"]["id"]

    created = await client.post("/org/schedules/weekly", json={
        "scope_level": "org", "scope_id": a_org_id, "day_of_week": 4,
        "open_time": "09:00:00", "close_time": "17:00:00",
    }, headers=a_headers)
    schedule_id = created.json()["data"]["id"]

    # Org B cannot read, update or delete org A's schedule by guessing ids
    assert (await client.get(
        f"/org/schedules/weekly/{schedule_id}", headers=b_headers
    )).status_code == 404
    assert (await client.put(
        f"/org/schedules/weekly/{schedule_id}",
        json={"open_time": "05:00:00"}, headers=b_headers,
    )).status_code == 404
    assert (await client.delete(
        f"/org/schedules/weekly/{schedule_id}", headers=b_headers
    )).status_code == 404

    # Org B creating a schedule pointing at org A's org-id scope fails too
    resp = await client.post("/org/schedules/weekly", json={
        "scope_level": "org", "scope_id": a_org_id, "day_of_week": 0,
        "open_time": "09:00:00", "close_time": "17:00:00",
    }, headers=b_headers)
    assert resp.status_code == 400


@pytest.mark.api
async def test_scope_target_ownership(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_approve_org(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}

    # scope_id belonging to another organization (counter id guess) → 404
    resp = await client.post("/org/schedules/weekly", json={
        "scope_level": "counter", "scope_id": 999999, "day_of_week": 0,
        "open_time": "09:00:00", "close_time": "17:00:00",
    }, headers=headers)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


@pytest.mark.api
async def test_schedule_exception_lifecycle_and_validation(
    client: AsyncClient, db_session: AsyncSession
):
    token = await _register_and_approve_org(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}
    org_id = (await client.get("/org/api/profile", headers=headers)).json()["data"]["id"]

    create = await client.post("/org/schedules/exceptions", json={
        "scope_level": "org", "scope_id": org_id,
        "exception_date": "2099-12-25",
        "exception_type": "holiday",
        "reason": "Christmas holiday",
    }, headers=headers)
    assert create.status_code == 200, create.text
    exc_id = create.json()["data"]["id"]

    # Duplicate date → 400
    dup = await client.post("/org/schedules/exceptions", json={
        "scope_level": "org", "scope_id": org_id,
        "exception_date": "2099-12-25",
        "exception_type": "emergency_closure",
        "reason": "Duplicate attempt",
    }, headers=headers)
    assert dup.status_code == 400
    assert "already exists" in dup.json()["detail"]

    # special_hours without window → 400
    bad = await client.post("/org/schedules/exceptions", json={
        "scope_level": "org", "scope_id": org_id,
        "exception_date": "2099-12-26",
        "exception_type": "special_hours",
        "reason": "Missing window",
    }, headers=headers)
    assert bad.status_code == 400

    # capacity_factor > 1 → 422 (schema bound)
    bad = await client.post("/org/schedules/exceptions", json={
        "scope_level": "org", "scope_id": org_id,
        "exception_date": "2099-12-26",
        "exception_type": "reduced_capacity",
        "reason": "Too large factor",
        "capacity_factor": 1.5,
    }, headers=headers)
    assert bad.status_code == 422

    # valid reduced_capacity → 200
    ok = await client.post("/org/schedules/exceptions", json={
        "scope_level": "org", "scope_id": org_id,
        "exception_date": "2099-12-26",
        "exception_type": "reduced_capacity",
        "reason": "Reduced staffing",
        "capacity_factor": 0.5,
    }, headers=headers)
    assert ok.status_code == 200

    # Deactivate via update
    upd = await client.put(
        f"/org/schedules/exceptions/{exc_id}",
        json={"is_active": False}, headers=headers,
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["is_active"] is False

    # Delete
    assert (await client.delete(
        f"/org/schedules/exceptions/{exc_id}", headers=headers
    )).status_code == 200


@pytest.mark.api
async def test_capacity_rule_lifecycle_and_validation(
    client: AsyncClient, db_session: AsyncSession
):
    token = await _register_and_approve_org(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}
    org_id = (await client.get("/org/api/profile", headers=headers)).json()["data"]["id"]

    create = await client.post("/org/schedules/capacity-rules", json={
        "scope_level": "org", "scope_id": org_id,
        "limit_type": "daily_token", "booking_source": "all", "max_count": 50,
    }, headers=headers)
    assert create.status_code == 200, create.text
    rule_id = create.json()["data"]["id"]

    # Duplicate (same scope/type/source) → 400
    dup = await client.post("/org/schedules/capacity-rules", json={
        "scope_level": "org", "scope_id": org_id,
        "limit_type": "daily_token", "booking_source": "all", "max_count": 60,
    }, headers=headers)
    assert dup.status_code == 400
    assert "already exists" in dup.json()["detail"]

    # Negative max_count → 422 (schema bound)
    neg = await client.post("/org/schedules/capacity-rules", json={
        "scope_level": "org", "scope_id": org_id,
        "limit_type": "slot_capacity", "max_count": -2,
    }, headers=headers)
    assert neg.status_code == 422

    # Update + delete
    upd = await client.put(
        f"/org/schedules/capacity-rules/{rule_id}",
        json={"max_count": 75}, headers=headers,
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["max_count"] == 75
    assert (await client.delete(
        f"/org/schedules/capacity-rules/{rule_id}", headers=headers
    )).status_code == 200


@pytest.mark.api
async def test_schedule_mutations_are_audited(
    client: AsyncClient, db_session: AsyncSession
):
    token = await _register_and_approve_org(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}
    org_id = (await client.get("/org/api/profile", headers=headers)).json()["data"]["id"]

    created = await client.post("/org/schedules/weekly", json={
        "scope_level": "org", "scope_id": org_id, "day_of_week": 1,
        "open_time": "09:00:00", "close_time": "17:00:00",
    }, headers=headers)
    schedule_id = created.json()["data"]["id"]
    await client.put(
        f"/org/schedules/weekly/{schedule_id}",
        json={"close_time": "18:00:00"}, headers=headers,
    )
    await client.delete(f"/org/schedules/weekly/{schedule_id}", headers=headers)

    logs = await client.get(
        "/org/api/audit-logs", headers=headers, params={"limit": 50}
    )
    assert logs.status_code == 200
    actions = [entry["action"] for entry in logs.json()["data"]]
    assert "schedule.weekly.create" in actions
    assert "schedule.weekly.update" in actions
    assert "schedule.weekly.delete" in actions
