"""
tests/api/test_org_schedule_ui.py
────────────────────────────────
Organization-panel schedule manager (Phase 3 roadmap UI bullet):
cookie-session view routes /org/schedule, /org/schedule/weekly/* and
/org/schedule/exception/* wired to schedule_service CRUD (Decision 17
honored: configuration rows only). Covers login gating, page rendering,
the weekly create/closed/reopen cycle, validation failures, deletes and
exception CRUD with type-specific field rules.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.admin_service import seed_default_admin
from app.services import schedule_service
from models.schedule_exception import ScheduleException
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule
import models  # noqa: F401

_seq = 0


def _next_email() -> str:
    global _seq
    _seq += 1
    return f"schui{_seq}@example.com"


async def _login_org(client: AsyncClient, db_session: AsyncSession) -> int:
    """Register + approve an org, log in via the HTML form (sets the
    org_token cookie); returns the org id."""
    global _seq
    _seq += 1
    await seed_default_admin(db_session)
    email = _next_email()
    resp = await client.post("/org/register", json={
        "name": f"Schedule UI Org {_seq}",
        "registration_number": f"SCHUI-{_seq:05d}",
        "org_type": "hospital",
        "address": "1 Schedule Way",
        "city": "Metro City",
        "state": "State",
        "email": email,
        "mobile": "9876543210",
        "password": "Password@123",
        "confirm_password": "Password@123",
    })
    assert resp.status_code == 200, resp.text
    org_id = resp.json()["data"]["id"]

    admin_login = await client.post("/auth/admin/login", json={
        "email": settings.admin_email, "password": settings.admin_password,
    })
    assert admin_login.status_code == 200
    approve = await client.post(
        f"/admin/organizations/{org_id}/approve",
        headers={"Authorization": f"Bearer {admin_login.json()['access_token']}"},
    )
    assert approve.status_code == 200

    login = await client.post(
        "/org/login/submit",
        data={"email": email, "password": "Password@123"},
        follow_redirects=False,
    )
    assert login.status_code == 302, login.text
    assert "org_token" in resp.cookies or "org_token" in client.cookies
    return org_id


async def _org_weekly(db_session: AsyncSession, org_id: int) -> list:
    return list((await db_session.execute(
        select(WeeklySchedule).where(
            WeeklySchedule.organization_id == org_id,
            WeeklySchedule.scope_level == ScheduleScopeLevel.ORGANIZATION,
        ).order_by(WeeklySchedule.day_of_week)
    )).scalars().all())


async def _org_exceptions(db_session: AsyncSession, org_id: int) -> list:
    return list((await db_session.execute(
        select(ScheduleException).where(
            ScheduleException.organization_id == org_id,
        )
    )).scalars().all())


def _post(client, url, payload=None):
    return client.post(url, json=payload if payload is not None else {})


@pytest.mark.api
async def test_schedule_page_requires_login(client: AsyncClient):
    resp = await client.get("/org/schedule", follow_redirects=False)
    assert resp.status_code == 302
    assert "/org/login" in resp.headers["location"]


@pytest.mark.api
async def test_schedule_page_renders_manager(
    client: AsyncClient, db_session: AsyncSession
):
    org_id = await _login_org(client, db_session)
    resp = await client.get("/org/schedule")
    assert resp.status_code == 200
    assert "Weekly Operating Hours" in resp.text
    assert "Holiday Calendar" in resp.text
    assert "Monday" in resp.text


@pytest.mark.api
async def test_weekly_save_create_closed_reopen(
    client: AsyncClient, db_session: AsyncSession
):
    org_id = await _login_org(client, db_session)

    # 1. Publish Tuesday hours
    resp = await _post(client, "/org/schedule/weekly/save", {
        "day_of_week": 1, "is_closed": False,
        "open_time": "10:30", "close_time": "15:00",
        "slot_interval_minutes": 30,
    })
    assert resp.status_code == 200, resp.text
    rows = await _org_weekly(db_session, org_id)
    assert len(rows) == 1 and rows[0].day_of_week == 1
    assert rows[0].open_time.isoformat() == "10:30:00"
    assert rows[0].close_time.isoformat() == "15:00:00"
    assert rows[0].slot_interval_minutes == 30 and not rows[0].is_closed

    # 2. Mark closed → hours cleared, row kept
    resp = await _post(client, "/org/schedule/weekly/save", {
        "day_of_week": 1, "is_closed": True,
    })
    assert resp.status_code == 200, resp.text
    rows = await _org_weekly(db_session, org_id)
    assert len(rows) == 1 and rows[0].is_closed and rows[0].open_time is None

    # 3. Reopen with fresh hours (closed rows clear the interval, so it is
    #    re-supplied — same as the panel's interval input)
    resp = await _post(client, "/org/schedule/weekly/save", {
        "day_of_week": 1, "is_closed": False,
        "open_time": "09:00", "close_time": "12:00",
        "slot_interval_minutes": 30,
    })
    assert resp.status_code == 200, resp.text
    rows = await _org_weekly(db_session, org_id)
    assert len(rows) == 1
    assert not rows[0].is_closed and rows[0].open_time.isoformat() == "09:00:00"
    assert rows[0].slot_interval_minutes == 30


@pytest.mark.api
async def test_weekly_save_validation_errors(
    client: AsyncClient, db_session: AsyncSession
):
    await _login_org(client, db_session)

    bad_hours = await _post(client, "/org/schedule/weekly/save", {
        "day_of_week": 2, "is_closed": False,
        "open_time": "15:00", "close_time": "09:00",
    })
    assert bad_hours.status_code == 400
    assert "open_time" in bad_hours.json()["error"]

    bad_dow = await _post(client, "/org/schedule/weekly/save", {
        "day_of_week": 7, "is_closed": True,
    })
    assert bad_dow.status_code == 400


@pytest.mark.api
async def test_weekly_delete_lifecycle(
    client: AsyncClient, db_session: AsyncSession
):
    org_id = await _login_org(client, db_session)

    unknown = await _post(client, "/org/schedule/weekly/delete", {"day_of_week": 3})
    assert unknown.status_code == 404

    save = await _post(client, "/org/schedule/weekly/save", {
        "day_of_week": 3, "is_closed": False,
        "open_time": "09:00", "close_time": "17:00",
    })
    assert save.status_code == 200
    assert len(await _org_weekly(db_session, org_id)) == 1

    deleted = await _post(client, "/org/schedule/weekly/delete", {"day_of_week": 3})
    assert deleted.status_code == 200
    assert await _org_weekly(db_session, org_id) == []

    again = await _post(client, "/org/schedule/weekly/delete", {"day_of_week": 3})
    assert again.status_code == 404


@pytest.mark.api
async def test_exception_create_and_duplicate(
    client: AsyncClient, db_session: AsyncSession
):
    org_id = await _login_org(client, db_session)

    resp = await _post(client, "/org/schedule/exception/create", {
        "exception_date": "2030-01-01", "exception_type": "holiday",
        "reason": "New Year",
    })
    assert resp.status_code == 200, resp.text
    rows = await _org_exceptions(db_session, org_id)
    assert len(rows) == 1
    assert rows[0].reason == "New Year"

    dup = await _post(client, "/org/schedule/exception/create", {
        "exception_date": "2030-01-01", "exception_type": "maintenance",
        "reason": "duplicate",
    })
    assert dup.status_code == 400
    assert "already exists" in dup.json()["error"]


@pytest.mark.api
async def test_exception_type_field_rules(
    client: AsyncClient, db_session: AsyncSession
):
    await _login_org(client, db_session)

    missing_window = await _post(client, "/org/schedule/exception/create", {
        "exception_date": "2030-02-02", "exception_type": "special_hours",
        "reason": "no window",
    })
    assert missing_window.status_code == 400
    assert "opens_at" in missing_window.json()["error"]

    missing_factor = await _post(client, "/org/schedule/exception/create", {
        "exception_date": "2030-02-02", "exception_type": "reduced_capacity",
        "reason": "no factor",
    })
    assert missing_factor.status_code == 400
    assert "capacity_factor" in missing_factor.json()["error"]

    no_reason = await _post(client, "/org/schedule/exception/create", {
        "exception_date": "2030-02-02", "exception_type": "holiday",
        "reason": "   ",
    })
    assert no_reason.status_code == 400

    missing_date = await _post(client, "/org/schedule/exception/create", {
        "exception_type": "holiday", "reason": "no date",
    })
    assert missing_date.status_code == 400


@pytest.mark.api
async def test_exception_delete_lifecycle(
    client: AsyncClient, db_session: AsyncSession
):
    org_id = await _login_org(client, db_session)

    foreign = await client.post(
        "/org/schedule/exception/999999/delete", follow_redirects=False
    )
    assert foreign.status_code == 404

    created = await _post(client, "/org/schedule/exception/create", {
        "exception_date": "2030-03-03", "exception_type": "reduced_capacity",
        "reason": "short staff", "capacity_factor": "0.5",
    })
    assert created.status_code == 200
    exc_id = created.json()["data"]["id"]

    deleted = await client.post(f"/org/schedule/exception/{exc_id}/delete")
    assert deleted.status_code == 200
    assert await _org_exceptions(db_session, org_id) == []

    again = await client.post(f"/org/schedule/exception/{exc_id}/delete")
    assert again.status_code == 404
