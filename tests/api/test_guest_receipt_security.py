"""
tests/api/test_guest_receipt_security.py
────────────────────────────────
Security regression tests for the HIGH-severity guest receipt authorization
finding (Final Acceptance Review, Nov 4 2026).

Original vulnerability (reproduced by the FIRST test here): the
`_assert_token_access` helper in `app/routers/tokens.py` skipped ALL access
checks while `token.user_id is None`, so ANY unauthenticated caller could
download a guest receipt PDF — which embeds `guest_name` — merely by guessing
the predictable numeric token id (GET /tokens/{id}/receipt). This violated the
project's own API-004 standard (no predictable-ID exposure) and QR-002's
no-enumeration gate.

Fix design:
- Guest receipts are authorized ONLY by a purpose-bound HMAC-signed receipt
  reference (`signed_qr.PURPOSE_RECEIPT`) issued with the booking and never
  by the numeric token id (no second signing system — the existing
  purpose-scoped subkey machinery from QR-002 is reused).
- User-owned receipts are authorized ONLY by the owning user's session.
- Every denial (missing/invalid/expired/tampered/mismatched ref on a guest
  token; anyone but the owner on a user token) returns the SAME uninformative
  404 as a nonexistent token, so status codes never reveal whether a probed
  id exists (QR-002 tracking precedent).
- Guest-downloadable receipts no longer embed the guest's name
  (PII protection; MASTER §22 receipt content does not require it).

All data is disposable in-memory test-database data; the real project
database is never touched.
"""

import time
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils import pdf_generator, signed_qr
from app.services import employee_service, org_service
from app.utils.jwt import create_access_token
from models.counter import Counter, CounterStatus
from models.organization import Organization
from models.service import Service


# ── Disposal fixtures (pattern from tests/api/test_phase4_channels.py) ───────


async def _make_org(db: AsyncSession) -> Organization:
    uid = uuid.uuid4().hex[:8]
    org = await org_service.register_organization(
        db,
        {
            "name": f"ReceiptSec Org {uid}",
            "registration_number": f"RS-{uid}",
            "org_type": "bank",
            "email": f"rs_{uid}@test.org",
            "mobile": "9800000000",
            "password": "Password123!",
            "address": "7 Secure Lane",
            "city": "Bengaluru",
            "state": "Karnataka",
        },
    )
    org.status = "approved"
    await db.commit()
    await db.refresh(org)
    return org


async def _make_counter(db: AsyncSession, org: Organization) -> Counter:
    counter = Counter(
        organization_id=org.id,
        counter_name=f"Counter-{uuid.uuid4().hex[:4]}",
        counter_number=1,
        token_prefix="RS",
        daily_limit=100,
        status=CounterStatus.ACTIVE,
    )
    db.add(counter)
    await db.commit()
    await db.refresh(counter)
    return counter


async def _make_service(db: AsyncSession, org: Organization) -> Service:
    service = Service(
        organization_id=org.id,
        name=f"Service-{uuid.uuid4().hex[:4]}",
        price=Decimal("0.00"),
        is_active=True,
    )
    db.add(service)
    await db.commit()
    await db.refresh(service)
    return service


@pytest.fixture
def receipt_dirs(tmp_path, monkeypatch):
    """Keep PDF/QR artifacts inside pytest tmp storage, never in the repo."""
    receipts = tmp_path / "receipts"
    qrs = tmp_path / "qr"
    monkeypatch.setattr(pdf_generator.settings, "receipt_dir", str(receipts))
    monkeypatch.setattr("app.config.settings.qr_code_dir", str(qrs))
    return receipts


@pytest.fixture
def receipt_spy(receipt_dirs, monkeypatch):
    """Capture the arguments the router passes into PDF generation."""
    captured: dict = {}
    original = pdf_generator.generate_token_receipt

    async def spy(**kwargs):
        captured.update(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(pdf_generator, "generate_token_receipt", spy)
    return captured


# ── Booking helpers ──────────────────────────────────────────────────────────


async def _book_guest_token(client: AsyncClient, db: AsyncSession) -> dict:
    """Book a kiosk (guest) token via the public channel; return the
    booking response payload (includes the signed receipt ref)."""
    org = await _make_org(db)
    counter = await _make_counter(db, org)
    service = await _make_service(db, org)
    resp = await client.post("/public/kiosk/book", json={
        "organization_id": org.id,
        "counter_id": counter.id,
        "service_id": service.id,
        "guest_name": "Secret Guest Name",
        "guest_mobile": "9870000000",
        "client_reference": f"sec_{uuid.uuid4().hex}",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _register_and_login_user(client: AsyncClient, email: str) -> str:
    await client.post("/user/register", json={
        "name": "Receipt Owner",
        "email": email,
        "mobile": "9812345678",
        "password": "OwnerPassword@123",
        "confirm_password": "OwnerPassword@123",
    })
    resp = await client.post("/auth/user/login", json={
        "email": email, "password": "OwnerPassword@123"
    })
    assert resp.status_code in (200, 302), resp.text
    return resp.cookies.get("user_token")


async def _book_user_token(client: AsyncClient, db: AsyncSession, uid: str | None = None):
    """Register a user and book a token; return (token_id, session cookie)."""
    uid = uid or uuid.uuid4().hex[:8]
    org = await _make_org(db)
    counter = await _make_counter(db, org)
    service = await _make_service(db, org)
    session = await _register_and_login_user(client, f"owner_{uid}@example.com")
    book = await client.post(
        "/user/tokens/book",
        cookies={"user_token": session},
        json={
            "organization_id": org.id,
            "counter_id": counter.id,
            "service_id": service.id,
        },
    )
    assert book.status_code == 200, book.text
    return book.json()["data"]["id"], session


def _flip_char(value: str) -> str:
    return "A" if value[0] != "A" else "B"


# ── The vulnerability reproduction & core guest-ref rules ────────────────────


@pytest.mark.api
async def test_regression_guest_receipt_by_numeric_id_is_rejected(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    """
    REGRESSION: reproduces the original vulnerability request — a bare
    `GET /tokens/{id}/receipt` with NO authentication and NO signed ref on a
    guest token. Before the fix this returned the guest's PDF (HIGH finding);
    after the fix it MUST be rejected with the uniform 404.
    """
    token = await _book_guest_token(client, db_session)
    assert token["user_id"] is None  # it is a guest token

    resp = await client.get(f"/tokens/{token['id']}/receipt")
    assert resp.status_code == 404


@pytest.mark.api
async def test_guest_receipt_with_empty_ref_is_rejected(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    """A numeric id plus an empty/absent ref is never sufficient."""
    token = await _book_guest_token(client, db_session)
    for ref in ("", None):
        url = f"/tokens/{token['id']}/receipt"
        if ref:
            url += f"?ref={ref}"
        resp = await client.get(url)
        assert resp.status_code == 404


@pytest.mark.api
async def test_guest_receipt_with_valid_signed_ref_succeeds(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    """The legitimate guest path: booking response ref → PDF download."""
    token = await _book_guest_token(client, db_session)
    assert token.get("receipt_ref"), "kiosk booking must return a receipt ref"

    resp = await client.get(
        f"/tokens/{token['id']}/receipt?ref={token['receipt_ref']}"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"


@pytest.mark.api
async def test_receipt_ref_is_opaque_and_not_a_plaintext_token_id(
    client: AsyncClient, db_session: AsyncSession
):
    """The ref is a two-part signed opaque value; distinctive token ids are
    never readable in the payload/signature (QR-002 opacity property on the
    new receipt purpose)."""
    token = await _book_guest_token(client, db_session)
    ref = token["receipt_ref"]
    assert "." in ref

    distinctive_id = 987654321
    probe_ref = signed_qr.create_token_ref(
        distinctive_id, token["organization_id"], signed_qr.PURPOSE_RECEIPT
    )
    assert str(distinctive_id) not in probe_ref


@pytest.mark.api
async def test_ref_for_another_token_does_not_authorize_this_token(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    token_a = await _book_guest_token(client, db_session)
    token_b = await _book_guest_token(client, db_session)

    resp = await client.get(
        f"/tokens/{token_b['id']}/receipt?ref={token_a['receipt_ref']}"
    )
    assert resp.status_code == 404
    assert resp.text != token_a.get("token_number", "") # no receipt content echo


@pytest.mark.api
async def test_forged_signature_is_rejected(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    token = await _book_guest_token(client, db_session)
    ref = token["receipt_ref"]
    payload_part, sig_part = ref.split(".", 1)
    forged = f"{payload_part}.{_flip_char(sig_part)}{sig_part[1:]}"

    resp = await client.get(f"/tokens/{token['id']}/receipt?ref={forged}")
    assert resp.status_code == 404


@pytest.mark.api
async def test_modified_payload_is_rejected(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    token = await _book_guest_token(client, db_session)
    ref = token["receipt_ref"]
    payload_part, sig_part = ref.split(".", 1)
    modified = f"{_flip_char(payload_part)}{payload_part[1:]}.{sig_part}"

    resp = await client.get(f"/tokens/{token['id']}/receipt?ref={modified}")
    assert resp.status_code == 404


@pytest.mark.api
async def test_track_purpose_ref_cannot_authorize_receipt_download(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    """Purpose binding: a perfectly valid TRACK ref (e.g. copied from the
    tracking QR) must NOT authorize a receipt download."""
    token = await _book_guest_token(client, db_session)
    track_ref = signed_qr.create_token_ref(
        token["id"], token["organization_id"], signed_qr.PURPOSE_TRACK
    )
    resp = await client.get(f"/tokens/{token['id']}/receipt?ref={track_ref}")
    assert resp.status_code == 404


@pytest.mark.api
async def test_expired_receipt_ref_is_rejected(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    """Expiry is part of the design (Decision 19, provisional TTL — reused)."""
    token = await _book_guest_token(client, db_session)
    expired_ref = signed_qr.create_token_ref(
        token["id"],
        token["organization_id"],
        signed_qr.PURPOSE_RECEIPT,
        ttl_seconds=50,
        now=time.time() - 100,  # issued in the past → already expired
    )
    status, _ = signed_qr.verify_token_ref(
        expired_ref, signed_qr.PURPOSE_RECEIPT
    )
    assert status == signed_qr.STATUS_EXPIRED

    resp = await client.get(f"/tokens/{token['id']}/receipt?ref={expired_ref}")
    assert resp.status_code == 404


@pytest.mark.api
async def test_garbage_refs_are_rejected_with_uniform_response(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    token = await _book_guest_token(client, db_session)
    baseline_url = f"/tokens/{token['id']}/receipt"
    for ref in ["1", "0", "f-p", "abcde.fghij", "..", "", "%%$.zz"]:
        resp = await client.get(f"{baseline_url}?ref={ref}")
        assert resp.status_code == 404


# ── User-owned token rules (unchanged behaviour made explicit) ───────────────


@pytest.mark.api
async def test_authenticated_owner_can_download_own_receipt(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    token_id, session = await _book_user_token(client, db_session)
    resp = await client.get(
        f"/tokens/{token_id}/receipt", cookies={"user_token": session}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"


@pytest.mark.api
async def test_user_owned_receipt_requires_owner_session(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    """A TRULY unauthenticated caller (separate client with no stored
    session cookie) with only the numeric id of a user-owned token is
    rejected. Sharing of a token id alone cannot grant receipt access."""
    from app.main import app

    token_id, _ = await _book_user_token(client, db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as fresh:
        resp = await fresh.get(f"/tokens/{token_id}/receipt")
    assert resp.status_code == 404


@pytest.mark.api
async def test_another_user_cannot_download_ones_receipt(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    token_id, _ = await _book_user_token(client, db_session)
    impostor_session = await _register_and_login_user(
        client, f"impostor_{uuid.uuid4().hex[:8]}@example.com"
    )
    resp = await client.get(
        f"/tokens/{token_id}/receipt",
        cookies={"user_token": impostor_session},
    )
    assert resp.status_code == 404


@pytest.mark.api
async def test_organization_and_employee_sessions_cannot_read_customer_receipts(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    """Org/employee authorization rules remain intact: a tenant session is
    NOT a customer-owner session and gains nothing on token downloads."""
    token_id, _ = await _book_user_token(client, db_session)
    org = await _make_org(db_session)

    org_resp = await client.get(
        f"/tokens/{token_id}/receipt",
        headers={"Authorization": f"Bearer {create_access_token(org.id, 'organization')}"},
    )
    assert org_resp.status_code == 404

    from models.branch import Branch

    uid = uuid.uuid4().hex[:8]
    branch = Branch(
        organization_id=org.id,
        name=f"Branch {uid}",
        branch_code=uid,
        address="Ground Floor",
        city="Bengaluru",
        state="Karnataka",
        pincode="560001",
    )
    db_session.add(branch)
    await db_session.flush()

    employee = await employee_service.create_employee(
        db_session,
        org.id,
        {
            "name": "Sec Operator",
            "email": f"op_{uid[:6]}@example.com",
            "employee_code": f"RS{uid[:4].upper()}",
            "branch_id": branch.id,
            "password": "SecurePassword@123",
        },
    )
    emp_resp = await client.get(
        f"/tokens/{token_id}/receipt",
        headers={"Authorization": f"Bearer {create_access_token(employee.id, 'employee')}"},
    )
    assert emp_resp.status_code == 404


@pytest.mark.api
async def test_missing_token_returns_404_unchanged(client: AsyncClient):
    """Pre-existing contract preserved: unknown ids still 404."""
    resp = await client.get("/tokens/999999/receipt")
    assert resp.status_code == 404


# ── Static-file bypass closure (the receipt file itself must not be public) ──


@pytest.mark.api
async def test_static_receipt_bypass_path_is_closed(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    """A guest receipt must not leak through its storage path either: the
    receipt file the endpoint writes (`receipts/receipt_token_{id}.pdf`, a
    predictable name derived from the same numeric id) must not be served
    from a static mount with no authorization."""
    from app.main import app

    token = await _book_guest_token(client, db_session)
    receipt = await client.get(
        f"/tokens/{token['id']}/receipt?ref={token['receipt_ref']}"
    )
    assert receipt.status_code == 200

    # Path bypass attempt — the raw filesystem layout of the downloadable PDF
    resp = await client.get(f"/receipts/receipt_token_{token['id']}.pdf")
    assert resp.status_code == 404  # not routed (static mount removed)

    # Phase 9 Workstream A1: the /qr_codes static mount is REMOVED too — its
    # predictable filenames token_{id}_org_{org}.png leaked any token's QR.
    qr_bypass = await client.get(
        f"/qr_codes/token_{token['id']}_org_{token['organization_id']}.png"
    )
    assert qr_bypass.status_code == 404  # not routed (static mount removed)

    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert "/receipts" not in route_paths          # mount removed
    assert "/qr_codes" not in route_paths          # mount removed (A1)


@pytest.mark.unit
def test_pdf_and_qr_files_are_never_served_statically():
    """Structural guarantee (independent of route order): the app mounts no
    static /receipts or /qr_codes surface; both artifacts are served only
    through the authorized /tokens endpoints."""
    from app.main import app

    mounted = {
        getattr(route, "path", None)
        for route in app.routes
        if route.__class__.__name__ == "Mount"
    }
    assert "/receipts" not in mounted
    assert "/qr_codes" not in mounted
    assert "/static" in mounted


# ── Guest PII protection ─────────────────────────────────────────────────────


@pytest.mark.api
async def test_guest_pdf_does_not_embed_guest_name(
    client: AsyncClient, db_session: AsyncSession, receipt_spy
):
    """The router must not pass the guest's name into a receipt that will be
    served to the bearer of a shareable ref (PII protection)."""
    token = await _book_guest_token(client, db_session)
    resp = await client.get(
        f"/tokens/{token['id']}/receipt?ref={token['receipt_ref']}"
    )
    assert resp.status_code == 200
    assert receipt_spy["guest_name"] is None  # ← PII withheld


@pytest.mark.api
async def test_guest_pdf_keeps_core_receipt_fields(
    client: AsyncClient, db_session: AsyncSession, receipt_spy
):
    """Protected info removal must not strip required receipt content
    (MASTER §22: org/counter/service/date/position/token + QR remain)."""
    token = await _book_guest_token(client, db_session)
    resp = await client.get(
        f"/tokens/{token['id']}/receipt?ref={token['receipt_ref']}"
    )
    assert resp.status_code == 200
    captured = receipt_spy
    assert captured["token_number"] == token["token_number"]
    assert captured["org_name"]
    assert captured["counter_name"]
    assert captured["service_name"]
    assert captured["booked_at"]
    assert captured["qr_code_path"]  # the live-tracking QR is preserved


@pytest.mark.api
async def test_owner_pdf_generation_still_accepts_a_guest_name_argument(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs, monkeypatch
):
    """Protected: the PII containment lives in the ROUTER policy — the
    generator's own capability is untouched (existing receipt generation
    tests continue to pass)."""
    from pathlib import Path

    monkeypatch.setattr(
        pdf_generator.settings, "receipt_dir", str(Path(receipt_dirs))
    )
    path = await pdf_generator.generate_token_receipt(
        token_id=777001,
        org_name="PII Capability Org",
        token_number="CAP-001",
        counter_name="Counter 1",
        service_name="Service",
        queue_position=1,
        booked_at=__import__("datetime").datetime(2026, 11, 4, 9, 0),
        guest_name="Generator Level Name",
    )
    assert Path(path).exists()


@pytest.mark.unit
def test_receipt_ref_purpose_is_bound_to_its_own_subkey():
    """Unit: a receipt ref verifies only under PURPOSE_RECEIPT (purpose-scoped
    subkeys — no cross-purpose rebinding)."""
    ref = signed_qr.create_token_ref(123, 7, signed_qr.PURPOSE_RECEIPT)
    status, payload = signed_qr.verify_token_ref(ref, signed_qr.PURPOSE_RECEIPT)
    assert status == signed_qr.STATUS_VALID
    assert payload.token_id == 123 and payload.org_id == 7

    for foreign in (signed_qr.PURPOSE_TRACK, signed_qr.PURPOSE_SCAN):
        status, _ = signed_qr.verify_token_ref(ref, foreign)
        assert status in (signed_qr.STATUS_TAMPERED, signed_qr.STATUS_INVALID)


# ── Phase 9 Workstream A1: QR endpoint authorization (no static bypass) ──────


@pytest.mark.api
async def test_guest_qr_by_numeric_id_alone_is_rejected(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    """A1 regression: bare `GET /tokens/{id}/qr` with no auth and no ref is
    rejected — the removed static /qr_codes mount must not have an
    endpoint-shaped equivalent."""
    token = await _book_guest_token(client, db_session)
    resp = await client.get(f"/tokens/{token['id']}/qr")
    assert resp.status_code == 404


@pytest.mark.api
async def test_guest_qr_with_valid_ref_succeeds(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    """The legitimate kiosk path: guest's own QR via the receipt ref."""
    token = await _book_guest_token(client, db_session)
    resp = await client.get(
        f"/tokens/{token['id']}/qr?ref={token['receipt_ref']}"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")


@pytest.mark.api
async def test_guest_qr_with_foreign_ref_is_rejected(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    token_a = await _book_guest_token(client, db_session)
    token_b = await _book_guest_token(client, db_session)
    resp = await client.get(
        f"/tokens/{token_b['id']}/qr?ref={token_a['receipt_ref']}"
    )
    assert resp.status_code == 404


@pytest.mark.api
async def test_owner_can_download_token_qr(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    token_id, session = await _book_user_token(client, db_session)
    resp = await client.get(
        f"/tokens/{token_id}/qr", cookies={"user_token": session}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")


@pytest.mark.api
async def test_fresh_client_cannot_download_user_qr(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    from app.main import app
    from httpx import ASGITransport

    token_id, _ = await _book_user_token(client, db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as fresh:
        resp = await fresh.get(f"/tokens/{token_id}/qr")
    assert resp.status_code == 404


# ── Phase 9 Workstream A2: /user/track/{token_id} ownership enforcement ──────


@pytest.mark.api
async def test_owner_sees_own_tracking_page(
    client: AsyncClient, db_session: AsyncSession, receipt_dirs
):
    token_id, session = await _book_user_token(client, db_session)
    resp = await client.get(
        f"/user/track/{token_id}", cookies={"user_token": session}
    )
    assert resp.status_code == 200


@pytest.mark.api
async def test_tracking_page_without_session_redirects_to_login(
    client: AsyncClient, db_session: AsyncSession
):
    """No session → portal login redirect (does not render any token data)."""
    from app.main import app
    from httpx import ASGITransport

    token_id, _ = await _book_user_token(client, db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as fresh:
        resp = await fresh.get(f"/user/track/{token_id}")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/user/login")


@pytest.mark.api
async def test_foreign_user_tracking_page_is_404(
    client: AsyncClient, db_session: AsyncSession
):
    token_id, _ = await _book_user_token(client, db_session)
    impostor = await _register_and_login_user(
        client, f"trackimp_{uuid.uuid4().hex[:8]}@example.com"
    )
    resp = await client.get(
        f"/user/track/{token_id}", cookies={"user_token": impostor}
    )
    assert resp.status_code == 404


@pytest.mark.api
async def test_guest_token_not_readable_via_numeric_tracking_page(
    client: AsyncClient, db_session: AsyncSession
):
    """Guest tokens (user_id NULL) are never exposed on the owner-portal
    numeric route — guests track through the signed-ref public flow."""
    token = await _book_guest_token(client, db_session)
    session = await _register_and_login_user(
        client, f"trackguest_{uuid.uuid4().hex[:8]}@example.com"
    )
    resp = await client.get(
        f"/user/track/{token['id']}", cookies={"user_token": session}
    )
    assert resp.status_code == 404


@pytest.mark.api
async def test_nonexistent_tracking_id_is_404(
    client: AsyncClient, db_session: AsyncSession
):
    session = await _register_and_login_user(
        client, f"tracknone_{uuid.uuid4().hex[:8]}@example.com"
    )
    resp = await client.get(
        "/user/track/9999999", cookies={"user_token": session}
    )
    assert resp.status_code == 404


@pytest.mark.unit
def test_user_track_page_has_no_public_numeric_access():
    """Structural: the user-portal track view requires the session redirect
    helper before any token lookup (A2 guarantee lives at code level)."""
    import inspect

    from app.routers import views_user

    source = inspect.getsource(views_user.track_page)
    assert "_get_user_or_redirect" in source
    assert "token.user_id != user.id" in source or "user.id" in source
