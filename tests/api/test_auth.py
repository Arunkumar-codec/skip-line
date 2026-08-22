"""tests/api/test_auth.py — Auth endpoint integration tests."""

import pytest
from httpx import AsyncClient


@pytest.mark.api
async def test_health_check(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.api
async def test_user_register(client: AsyncClient):
    resp = await client.post("/user/register", json={
        "name": "Test User",
        "email": "testuser@example.com",
        "mobile": "9999999999",
        "password": "Password@123",
        "confirm_password": "Password@123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["email"] == "testuser@example.com"


@pytest.mark.api
async def test_user_register_duplicate_email(client: AsyncClient):
    payload = {
        "name": "Dup User",
        "email": "dup@example.com",
        "mobile": "9999999998",
        "password": "Password@123",
        "confirm_password": "Password@123",
    }
    await client.post("/user/register", json=payload)
    resp = await client.post("/user/register", json=payload)
    assert resp.status_code == 400


@pytest.mark.api
async def test_user_login_success(client: AsyncClient):
    await client.post("/user/register", json={
        "name": "Login Test",
        "email": "logintest@example.com",
        "mobile": "9999999997",
        "password": "Password@123",
        "confirm_password": "Password@123",
    })
    resp = await client.post("/auth/user/login", json={
        "email": "logintest@example.com",
        "password": "Password@123",
    })
    assert resp.status_code == 200
    assert "access_token" in resp.json()
    assert resp.json()["role"] == "user"


@pytest.mark.api
async def test_user_login_wrong_password(client: AsyncClient):
    resp = await client.post("/auth/user/login", json={
        "email": "logintest@example.com",
        "password": "WrongPassword",
    })
    assert resp.status_code == 401


@pytest.mark.api
async def test_org_register(client: AsyncClient):
    resp = await client.post("/org/register", json={
        "name": "Test Hospital",
        "registration_number": "TEST-HOSP-001",
        "org_type": "hospital",
        "address": "123 Test Road",
        "city": "Bengaluru",
        "state": "Karnataka",
        "email": "testhospital@example.com",
        "mobile": "8012345678",
        "password": "Password@123",
        "confirm_password": "Password@123",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "pending"


@pytest.mark.api
async def test_logout(client: AsyncClient):
    resp = await client.post("/auth/logout")
    assert resp.status_code == 200
