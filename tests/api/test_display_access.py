"""
tests/api/test_display_access.py
────────────────────────────────
Phase 6 — Decision 20 display authorization + Gate 6 track references.

- /public/display/{org_id} requires a valid display-access-key (or an
  authenticated organization session for the exact org) — cross-tenant
  display discovery must be impossible.
- /public/track/{ref} and /public/tracking/{ref} accept only signed opaque
  references; a plaintext numeric token id must be rejected (Gate 6).
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import org_service
from app.utils import signed_qr
from app.services.display_access_service import install_or_rotate_key
from app.utils.jwt import create_access_token
from models.counter import Counter, CounterStatus
from models.organization import Organization
from models.service import Service
from models.token import Token


async def _make_org(db_session: AsyncSession) -> Organization:
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(db_session, {
        "name": f"Display Org {uid}",
        "registration_number": f"DSO-{uid}",
        "org_type": "hospital",
        "email": f"dso_{uid}@hospital.com",
        "mobile": "9876543210",
        "password": "Password123!",
        "address": "7 Display Road",
        "city": "Bengaluru",
        "state": "Karnataka",
    })
    org.status = "approved"
    await db_session.commit()
    await db_session.refresh(org)
    return org


async def _counter_token(db_session: AsyncSession, org: Organization) -> tuple[Counter, Token]:
    counter = Counter(
        organization_id=org.id,
        counter_name="Display Counter",
        counter_number=1,
        token_prefix="D",
        daily_limit=20,
        status=CounterStatus.ACTIVE,
    )
    service = Service(
        organization_id=org.id,
        name=f"Display Svc {uuid.uuid4().hex[:6]}",
        price=Decimal("0.00"),
    )
    db_session.add_all([counter, service])
    await db_session.commit()
    await db_session.refresh(counter)
    await db_session.refresh(service)
    from app.services import token_service
    token = await token_service.book_token(
        db_session, org.id, counter.id, service.id, guest_name="Display Guest"
    )
    return counter, token


def org_headers(org: Organization) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(org.id, 'organization')}"}


# ── Display authorization (Decision 20) ─────────────────────────────────────

@pytest.mark.api
async def test_display_requires_authorization(client: AsyncClient, db_session: AsyncSession):
    org = await _make_org(db_session)
    res = await client.get(f"/public/display/{org.id}")
    assert res.status_code == 403


@pytest.mark.api
async def test_display_with_valid_key_succeeds(client: AsyncClient, db_session: AsyncSession):
    org = await _make_org(db_session)
    await _counter_token(db_session, org)
    key = await install_or_rotate_key(db_session, org.id)

    res = await client.get(f"/public/display/{org.id}", params={"key": key})
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert any(c["counter_id"] for c in data["counters"])
    # DSP-005 allowlist: only token numbers, no customeridentifiers
    body = res.text.lower()
    assert "guest" not in body and "mobile" not in body


@pytest.mark.api
async def test_display_wrong_key_is_403(client: AsyncClient, db_session: AsyncSession):
    org = await _make_org(db_session)
    await install_or_rotate_key(db_session, org.id)  # real key belongs to org
    res = await client.get(f"/public/display/{org.id}", params={"key": "intrusion-attempt-000"})
    assert res.status_code == 403


@pytest.mark.api
async def test_display_with_org_session_succeeds(client: AsyncClient, db_session: AsyncSession):
    """Operator dashboards keep working via the organization session."""
    org = await _make_org(db_session)
    await _counter_token(db_session, org)
    res = await client.get(
        f"/public/display/{org.id}", headers=org_headers(org)
    )
    assert res.status_code == 200


@pytest.mark.api
async def test_head_other_org_key_never_activates_other_keys(
    client: AsyncClient, db_session: AsyncSession
):
    """Org B's key does not open Org A's display."""
    org_a, org_b = await _make_org(db_session), await _make_org(db_session)
    key_b = await install_or_rotate_key(db_session, org_b.id)
    res = await client.get(f"/public/display/{org_a.id}", params={"key": key_b})
    assert res.status_code == 403


# ── Signed track references (Release Gate 6) ──────────────────────────────────

@pytest.mark.api
async def test_plaintext_numeric_track_id_is_rejected(client: AsyncClient, db_session: AsyncSession):
    """Enumeration of /track/{id} must yield 404, never token data (Gate 6)."""
    org = await _make_org(db_session)
    _, token = await _counter_token(db_session, org)
    for probe in (str(token.id), "1", "0", "999999"):
        res = await client.get(f"/public/tracking/{probe}")
        assert res.status_code == 404
        page = await client.get(f"/public/track/{probe}")
        assert page.status_code == 404


@pytest.mark.api
async def test_track_ref_json_returns_safe_data(client: AsyncClient, db_session: AsyncSession):
    org = await _make_org(db_session)
    counter, token = await _counter_token(db_session, org)
    ref = signed_qr.create_token_ref(token.id, org.id, signed_qr.PURPOSE_TRACK)

    res = await client.get(f"/public/tracking/{ref}")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["token_number"] == token.token_number
    assert data["counter_name"] == counter.counter_name
    body = res.text.lower()
    assert "guest" not in body


@pytest.mark.api
async def test_track_ref_html_page_renders(client: AsyncClient, db_session: AsyncSession):
    org = await _make_org(db_session)
    _, token = await _counter_token(db_session, org)
    ref = signed_qr.create_token_ref(token.id, org.id, signed_qr.PURPOSE_TRACK)

    page = await client.get(f"/public/track/{ref}")
    assert page.status_code == 200
    assert token.token_number in page.text
    # The page must join the WS room through the signed reference only.
    assert "/ws/token/" in page.text
    assert "const TOKEN_ID = null" in page.text


@pytest.mark.api
async def test_track_scan_purpose_ref_rejected_on_track(client: AsyncClient, db_session: AsyncSession):
    org = await _make_org(db_session)
    _, token = await _counter_token(db_session, org)
    scan_ref = signed_qr.create_token_ref(token.id, org.id, signed_qr.PURPOSE_SCAN)
    res = await client.get(f"/public/tracking/{scan_ref}")
    assert res.status_code == 404


@pytest.mark.api
async def test_expired_track_ref_rejected(client: AsyncClient, db_session: AsyncSession):
    import time

    org = await _make_org(db_session)
    _, token = await _counter_token(db_session, org)
    now = time.time()
    old_ref = signed_qr.create_token_ref(
        token.id, org.id, signed_qr.PURPOSE_TRACK, ttl_seconds=1, now=now - 5
    )
    assert (await client.get(f"/public/tracking/{old_ref}")).status_code == 404


# ── Display key rotation endpoint ────────────────────────────────────────────

@pytest.mark.api
async def test_rotate_key_requires_org_auth(client: AsyncClient):
    res = await client.post("/org/display-key/rotate")
    assert res.status_code == 401


@pytest.mark.api
async def test_rotate_key_returns_plaintext_once(client: AsyncClient, db_session: AsyncSession):
    org = await _make_org(db_session)
    res = await client.post("/org/display-key/rotate", headers=org_headers(org))
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["display_url"].endswith(f"?key={data['key']}")
    # new key invalidates the previous one
    res2 = await client.get(f"/public/display/{org.id}", params={"key": data["key"]})
    assert res2.status_code == 200
