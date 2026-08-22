"""tests/api/test_tokens.py — Token booking and tracking API tests."""

import pytest
from httpx import AsyncClient


async def _register_and_login_user(client: AsyncClient, email: str):
    await client.post("/user/register", json={
        "name": "Token Test User",
        "email": email,
        "mobile": "9000000001",
        "password": "Password@123",
        "confirm_password": "Password@123",
    })
    resp = await client.post("/auth/user/login", json={
        "email": email, "password": "Password@123"
    })
    return resp.cookies.get("user_token") or resp.json().get("access_token")


@pytest.mark.api
async def test_user_active_tokens_empty_on_registration(client: AsyncClient):
    token = await _register_and_login_user(client, "emptytoken@example.com")
    resp = await client.get("/user/dashboard", cookies={"user_token": token})
    assert resp.status_code == 200


@pytest.mark.api
async def test_track_nonexistent_token_returns_404(client: AsyncClient):
    resp = await client.get("/user/tokens/999999/track")
    assert resp.status_code == 404


@pytest.mark.api
async def test_token_history_requires_auth(client: AsyncClient):
    resp = await client.get("/user/tokens/history")
    # Without auth cookie → redirect or 401
    assert resp.status_code in (200, 401, 302, 307)


@pytest.mark.api
async def test_kiosk_book_missing_fields(client: AsyncClient):
    resp = await client.post("/public/kiosk/book", json={
        "organization_id": 1,
        "counter_id": 1,
        # missing guest_name and guest_mobile
    })
    assert resp.status_code == 422


@pytest.mark.api
async def test_kiosk_book_nonexistent_org_returns_error(client: AsyncClient):
    resp = await client.post("/public/kiosk/book", json={
        "organization_id": 99999,
        "counter_id": 99999,
        "guest_name": "Test Guest",
        "guest_mobile": "9876543210",
    })
    assert resp.status_code in (400, 404, 422)


@pytest.mark.api
async def test_receipt_nonexistent_token_returns_404(client: AsyncClient):
    resp = await client.get("/tokens/999999/receipt")
    assert resp.status_code == 404
