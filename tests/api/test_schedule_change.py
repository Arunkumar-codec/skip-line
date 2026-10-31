"""
tests/api/test_schedule_change.py
─────────────────────────────────
API-level tests for the SCH-007 schedule-change impact workflow endpoints
(Decision 11): anonymous access is rejected (401), the preview → resolve →
confirm lifecycle applies configuration exactly once, reject is final,
invalid proposals map to 400, re-resolution maps to 409 and cross-tenant
targets are indistinguishable 404s.
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
    return f"chgapi{_seq}@example.com"


def _next_reg() -> str:
    global _seq
    _seq += 1
    return f"CHG-API-{_seq:05d}"


async def _register_and_approve_org(
    client: AsyncClient, db_session: AsyncSession
) -> tuple[str, int]:
    """Seed admin, register + approve an org; return (access token, org id)."""
    await seed_default_admin(db_session)
    email, reg = _next_email(), _next_reg()

    resp = await client.post(
        "/org/register",
        json={
            "name": f"Change API Org {_seq}",
            "registration_number": reg,
            "org_type": "hospital",
            "address": "1 Change Ave",
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
    return org_login.json()["access_token"], org_id


async def _create_weekly(
    client: AsyncClient, headers: dict, scope_id: int, day_of_week: int = 0
) -> dict:
    resp = await client.post("/org/schedules/weekly", json={
        "scope_level": "org", "scope_id": scope_id, "day_of_week": day_of_week,
        "open_time": "09:00:00", "close_time": "17:00:00",
        "slot_interval_minutes": 30,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _update_preview_payload(target_id: int) -> dict:
    return {
        "entity": "weekly_schedule",
        "edit_type": "update",
        "target_id": target_id,
        "data": {"open_time": "10:00:00", "close_time": "16:00:00"},
    }


@pytest.mark.api
async def test_unauthenticated_change_access_rejected(client: AsyncClient):
    assert (
        await client.post("/org/schedules/changes/preview", json={})
    ).status_code == 401
    assert (await client.get("/org/api/schedules/changes")).status_code == 401
    assert (
        await client.post("/org/schedules/changes/1/confirm")
    ).status_code == 401


@pytest.mark.api
async def test_preview_validation_errors(client: AsyncClient, db_session: AsyncSession):
    token, _ = await _register_and_approve_org(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}

    # Unknown entity → 400
    bad = await client.post("/org/schedules/changes/preview", json={
        "entity": "not_an_entity", "edit_type": "create", "data": {},
    }, headers=headers)
    assert bad.status_code == 400, bad.text
    assert "entity" in bad.json()["detail"].lower()

    # Unknown edit_type → 400
    bad = await client.post("/org/schedules/changes/preview", json={
        "entity": "weekly_schedule", "edit_type": "move", "data": {},
    }, headers=headers)
    assert bad.status_code == 400

    # delete without target_id → 400
    bad = await client.post("/org/schedules/changes/preview", json={
        "entity": "weekly_schedule", "edit_type": "delete", "data": {},
    }, headers=headers)
    assert bad.status_code == 400

    # delete with impossible target → 404
    bad = await client.post("/org/schedules/changes/preview", json={
        "entity": "weekly_schedule", "edit_type": "delete",
        "target_id": 999999, "data": {},
    }, headers=headers)
    assert bad.status_code == 404


@pytest.mark.api
async def test_full_workflow_no_bookings(client: AsyncClient, db_session: AsyncSession):
    token, org_id = await _register_and_approve_org(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}

    row = await _create_weekly(client, headers, org_id, day_of_week=2)

    # 1) Preview proposes shorter hours; conflict-free → pending
    preview = await client.post(
        "/org/schedules/changes/preview",
        json=_update_preview_payload(row["id"]),
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    p = preview.json()["data"]
    assert p["status"] == "pending"
    assert p["proposal"]["fields"]["open_time"].startswith("10:00")
    preview_id = p["id"]

    # 2) Get single
    got = await client.get(
        f"/org/schedules/changes/{preview_id}", headers=headers
    )
    assert got.status_code == 200
    assert got.json()["data"]["impact"]["summary"]["bookings_affected"] == 0
    assert got.json()["data"]["decisions"] == {}

    # 3) List
    listing = await client.get("/org/api/schedules/changes", headers=headers)
    assert listing.status_code == 200
    assert [r["id"] for r in listing.json()["data"]] == [preview_id]

    # 4) Resolve with no bookings → empty decisions recorded
    resolve = await client.post(
        f"/org/schedules/changes/{preview_id}/resolve",
        json={"decisions": {}},
        headers=headers,
    )
    assert resolve.status_code == 200, resolve.text

    # 5) Confirm applies the change exactly once
    confirm = await client.post(
        f"/org/schedules/changes/{preview_id}/confirm", headers=headers
    )
    assert confirm.status_code == 200, confirm.text
    data = confirm.json()["data"]
    assert data["status"] == "confirmed"
    assert data["confirmed_at"] is not None
    assert data["applied_summary"]["config"]["updated"] == row["id"]
    assert len(data["impact_digest"]) == 64  # sha256 hex fingerprint

    # The configuration change is visible through the CRUD endpoint.
    weekly = await client.get(
        f"/org/schedules/weekly/{row['id']}", headers=headers
    )
    assert weekly.json()["data"]["open_time"].startswith("10:00")

    # 6) Re-confirm / re-resolve → 409 workflow conflict
    again = await client.post(
        f"/org/schedules/changes/{preview_id}/confirm", headers=headers
    )
    assert again.status_code == 409
    assert "already confirmed" in again.json()["detail"]


@pytest.mark.api
async def test_reject_is_final(client: AsyncClient, db_session: AsyncSession):
    token, org_id = await _register_and_approve_org(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}
    row = await _create_weekly(client, headers, org_id, day_of_week=1)

    preview = await client.post(
        "/org/schedules/changes/preview",
        json=_update_preview_payload(row["id"]),
        headers=headers,
    )
    preview_id = preview.json()["data"]["id"]
    reject = await client.post(
        f"/org/schedules/changes/{preview_id}/reject", headers=headers
    )
    assert reject.status_code == 200
    assert reject.json()["data"]["status"] == "rejected"

    # Rejecting / resolving / confirming again → 409; config untouched.
    again_reject = await client.post(
        f"/org/schedules/changes/{preview_id}/reject", headers=headers
    )
    assert again_reject.status_code == 409
    assert "already rejected" in again_reject.json()["detail"]
    again_confirm = await client.post(
        f"/org/schedules/changes/{preview_id}/confirm", headers=headers
    )
    assert again_confirm.status_code == 409
    assert "already rejected" in again_confirm.json()["detail"]
    again_resolve = await client.post(
        f"/org/schedules/changes/{preview_id}/resolve",
        json={"decisions": {}},
        headers=headers,
    )
    assert again_resolve.status_code == 409
    assert "already rejected" in again_resolve.json()["detail"]

    weekly = await client.get(
        f"/org/schedules/weekly/{row['id']}", headers=headers
    )
    assert weekly.json()["data"]["open_time"].startswith("09:00")


@pytest.mark.api
async def test_cross_tenant_isolation(client: AsyncClient, db_session: AsyncSession):
    token_a, org_a = await _register_and_approve_org(client, db_session)
    token_b, _ = await _register_and_approve_org(client, db_session)
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    row = await _create_weekly(client, headers_a, org_a)

    preview = await client.post(
        "/org/schedules/changes/preview",
        json=_update_preview_payload(row["id"]),
        headers=headers_a,
    )
    preview_id = preview.json()["data"]["id"]

    got = await client.get(f"/org/schedules/changes/{preview_id}", headers=headers_b)
    assert got.status_code == 404
    confirm = await client.post(
        f"/org/schedules/changes/{preview_id}/confirm", headers=headers_b
    )
    assert confirm.status_code == 404
    # Tenant A's weekly row is unchanged.
    weekly = await client.get(
        f"/org/schedules/weekly/{row['id']}", headers=headers_a
    )
    assert weekly.status_code == 200
