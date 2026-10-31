"""
tests/api/test_public_slots.py
──────────────────────────────
API-level tests for the public slot-ladder endpoint
`GET /public/organizations/{org_id}/slots` (SCH-009 rendering): tenant-
timezone slot times from the stored UTC pairs, same-day cutoff, horizon
guards, tenant filter gates and 404s.
"""

import datetime as dt
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import schedule_service
from models.counter import Counter, CounterStatus, QueueMode
from models.organization import Organization
from models.weekly_schedule import ScheduleScopeLevel, WeeklySchedule
import models  # noqa: F401


def _success(resp, status_code: int = 200):
    assert resp.status_code == status_code, resp.text
    return resp.json()


async def _seed_slot_org(db_session: AsyncSession, tz: str = "Asia/Kolkata"):
    """Approved org + one ACTIVE slot-mode counter with published 10-14
    hours; returns (org, counter) with rows flushed (client shares the
    session)."""
    global _seed
    _seed += 1
    name = f"Ladder API Org {_seed}"
    org = Organization(
        name=name,
        registration_number=f"LAD-{_seed:05d}-{abs(hash(name)) % 10**4}",
        org_type="hospital",
        address="1 Ladder Way",
        city="Metro City",
        state="State",
        email=f"ladderapi{_seed}@example.com",
        mobile="9000000001",
        password_hash="x",
        status="approved",
        timezone=tz,
    )
    db_session.add(org)
    await db_session.flush()
    counter = Counter(
        organization_id=org.id,
        counter_name=f"Ladder API Counter {_seed}",
        counter_number=(_seed % 90) + 1,
        token_prefix=f"LA{_seed}",
        queue_mode=QueueMode.SLOTS,
        status=CounterStatus.ACTIVE,
    )
    db_session.add(counter)
    await db_session.flush()
    for dow in range(7):
        db_session.add(WeeklySchedule(
            organization_id=org.id,
            scope_level=ScheduleScopeLevel.ORGANIZATION,
            scope_id=org.id,
            day_of_week=dow,
            open_time=time(10, 0),
            close_time=time(14, 0),
            slot_interval_minutes=60,
        ))
    await db_session.flush()
    return org, counter


_seed = 0


def _freeze_before_open(monkeypatch):
    """Consistent frozen clock: tenant today + 09:00 IST (= 03:30 UTC,
    same UTC date), before every operating hour."""
    frozen_today = date.today()
    frozen_now = dt.datetime.combine(
        frozen_today, time(9, 0), tzinfo=ZoneInfo("Asia/Kolkata")
    ).astimezone(dt.timezone.utc)
    monkeypatch.setattr(schedule_service, "get_tenant_today",
                        lambda _o, _b: frozen_today)
    monkeypatch.setattr(schedule_service, "_utcnow", lambda: frozen_now)
    return frozen_today


@pytest.mark.api
async def test_slot_ladder_happy_path(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    org, counter = await _seed_slot_org(db_session)
    _freeze_before_open(monkeypatch)
    ladder_day = date.today() + timedelta(days=3)

    # Generate via the endpoint itself (lazy generation is part of the
    # same idempotent contract the booking flow uses).
    body = _success(await client.get(f"/public/organizations/{org.id}/slots",
                                     params={"date": ladder_day.isoformat()}))
    data = body["data"]
    assert data["date"] == ladder_day.isoformat()
    assert data["timezone_name"] == "Asia/Kolkata"
    assert data["state"] == "available"
    assert data["opens_at"] == "10:00:00"
    assert len(data["slots"]) == 4
    first = data["slots"][0]
    assert first["counter_id"] == counter.id
    assert first["start_local"] == "10:00:00"      # rendered IANA-local
    assert first["end_local"] == "11:00:00"
    assert first["start_utc"].startswith(
        f"{ladder_day.isoformat()}T04:30"          # absolute UTC instant
    )
    assert first["remaining"] > 0 and first["is_bookable"] is True
    # Second call is stable + idempotent (no duplicates)
    again = _success(await client.get(f"/public/organizations/{org.id}/slots",
                                      params={"date": ladder_day.isoformat()}))
    assert [s["id"] for s in again["data"]["slots"]] == \
        [s["id"] for s in data["slots"]]


@pytest.mark.api
async def test_slot_ladder_unknown_org_and_bad_date(client: AsyncClient):
    assert _success(await client.get(
        "/public/organizations/999999999/slots"), 404
    )  # organization not found → 404
    resp = await client.get(
        "/public/organizations/999999999/slots",
        params={"date": "not-a-date"},
    )
    assert resp.status_code == 422                 # ISO date parse failure


@pytest.mark.api
async def test_slot_ladder_horizon_guard(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    org, _ = await _seed_slot_org(db_session)
    _freeze_before_open(monkeypatch)
    far = date.today() + timedelta(
        days=schedule_service.MAX_ADVANCE_BOOKING_DAYS + 1
    )
    resp = await client.get(f"/public/organizations/{org.id}/slots",
                            params={"date": far.isoformat()})
    _success(resp, 400)


@pytest.mark.api
async def test_slot_ladder_counter_filter_unknown_is_404(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    org, counter = await _seed_slot_org(db_session)
    _freeze_before_open(monkeypatch)
    ladder_day = date.today() + timedelta(days=3)

    _success(await client.get(
        f"/public/organizations/{org.id}/slots",
        params={"date": ladder_day.isoformat(), "counter_id": 999999999},
    ), 404)  # cross-tenant / unknown counter is indistinguishable → 404

    known = _success(await client.get(
        f"/public/organizations/{org.id}/slots",
        params={"date": ladder_day.isoformat(), "counter_id": counter.id},
    ))
    assert len(known["data"]["slots"]) == 4
    assert {s["counter_id"] for s in known["data"]["slots"]} == {counter.id}
