"""tests/api/test_organizations.py — Organization lifecycle API tests."""

import pytest
from httpx import AsyncClient


ORG_PAYLOAD = {
    "name": "General City Hospital",
    "registration_number": "GCH-2025-001",
    "org_type": "hospital",
    "address": "1 Hospital Road, Bengaluru",
    "city": "Bengaluru",
    "state": "Karnataka",
    "email": "gch@example.com",
    "mobile": "8011223344",
    "password": "Hospital@123",
    "confirm_password": "Hospital@123",
}


@pytest.mark.api
async def test_org_register_creates_pending(client: AsyncClient):
    resp = await client.post("/org/register", json=ORG_PAYLOAD)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["status"] == "pending"


@pytest.mark.api
async def test_org_register_duplicate_email_rejected(client: AsyncClient):
    await client.post("/org/register", json=ORG_PAYLOAD)
    resp = await client.post("/org/register", json=ORG_PAYLOAD)
    assert resp.status_code == 400


@pytest.mark.api
async def test_org_login_blocked_while_pending(client: AsyncClient):
    await client.post("/org/register", json={**ORG_PAYLOAD, "email": "pending@example.com", "registration_number": "PEND-001"})
    resp = await client.post("/auth/org/login", json={
        "email": "pending@example.com",
        "password": "Hospital@123",
    })
    # Should fail because org is pending
    assert resp.status_code == 401


@pytest.mark.api
async def test_public_org_search_empty(client: AsyncClient):
    resp = await client.get("/user/organizations/search?q=nonexistentorg12345")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.api
async def test_public_org_services_endpoint(client: AsyncClient):
    # Non-existent org returns empty list or 404
    resp = await client.get("/public/organizations/99999/services")
    assert resp.status_code in (200, 404)


@pytest.mark.api
async def test_org_password_mismatch_rejected(client: AsyncClient):
    payload = {**ORG_PAYLOAD, "email": "mismatch@example.com",
               "registration_number": "MM-001", "confirm_password": "Wrong@123"}
    resp = await client.post("/org/register", json=payload)
    assert resp.status_code == 422   # Pydantic validation error
