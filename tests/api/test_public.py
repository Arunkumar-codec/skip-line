"""tests/api/test_public.py — Public endpoint tests (no auth required)."""

import pytest
from httpx import AsyncClient


@pytest.mark.api
async def test_health_check(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.api
async def test_landing_page_returns_html(client: AsyncClient):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert b"Skip Line" in resp.content


@pytest.mark.api
async def test_display_screen_nonexistent_org(client: AsyncClient):
    resp = await client.get("/public/display/999999")
    assert resp.status_code == 200
    data = resp.json()
    assert "counters" in data["data"]
    assert data["data"]["counters"] == []


@pytest.mark.api
async def test_track_nonexistent_token(client: AsyncClient):
    resp = await client.get("/public/track/999999")
    assert resp.status_code == 404


@pytest.mark.api
async def test_kiosk_book_validation(client: AsyncClient):
    # Missing required fields → 422
    resp = await client.post("/public/kiosk/book", json={
        "organization_id": 1,
    })
    assert resp.status_code == 422


@pytest.mark.api
async def test_org_search_returns_paginated(client: AsyncClient):
    resp = await client.get("/user/organizations/search")
    assert resp.status_code == 200
    body = resp.json()
    assert "total" in body
    assert "data" in body
    assert isinstance(body["data"], list)


@pytest.mark.api
async def test_public_org_counters_nonexistent(client: AsyncClient):
    resp = await client.get("/public/organizations/999999/counters")
    # Either 200 with empty list or 404
    assert resp.status_code in (200, 404)


@pytest.mark.api
async def test_user_login_page_returns_html(client: AsyncClient):
    resp = await client.get("/user/login")
    assert resp.status_code == 200
    assert b"Skip Line" in resp.content


@pytest.mark.api
async def test_org_login_page_returns_html(client: AsyncClient):
    resp = await client.get("/org/login")
    assert resp.status_code == 200
    assert b"Skip Line" in resp.content


@pytest.mark.api
async def test_admin_login_page_returns_html(client: AsyncClient):
    resp = await client.get("/admin/login")
    assert resp.status_code == 200
    assert b"Skip Line" in resp.content
