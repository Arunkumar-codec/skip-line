"""
tests/api/test_admin_finance_ui.py
──────────────────────────────────
Phase 5 Admin Financial Management interface.

Covers the /admin/finance page (cookie-session view route) and the
admin finance APIs it operates (all pre-existing /admin endpoints +
the two new read-only listings):

  - Login gate and page rendering (payouts / risk flags / bank
    verification queue / ledger-derived overview).
  - FIN-001: payout hold (reason mandatory), release and authorised
    disbursement with immutable ledger posting.
  - FIN-003/004: risk flag review resolution via the admin API.
  - FIN-002: new bank-account listing endpoint + verification lift.
  - LED-004: /admin/api/finance/overview derives per-organization figures
    from ledger entries (no business logic in the page).
"""

import pytest
from httpx import AsyncClient
from decimal import Decimal
from sqlalchemy import select

from app.config import settings
from app.services.admin_service import seed_default_admin
from app.services import (
    bank_account_service,
    financial_risk_service,
    ledger_service,
    payout_service,
)
from models.counter import Counter
from models.finance import LedgerEntry, LedgerEntryType, PayoutStatus, RefundStatus
from models.organization import Organization, OrgStatus
from models.payment import Payment, PaymentMethod, PaymentStatus
from models.service import Service
from models.token import Token, TokenStatus
import models  # noqa: F401


# ── Fixtures & helpers ────────────────────────────────────────────────────────

_seq = 0


def _next_email() -> str:
    global _seq
    _seq += 1
    return f"admfin{_seq}@example.com"


async def _make_org(db_session) -> Organization:
    global _seq
    _seq += 1
    name = f"Admin Finance Org {_seq}"
    org = Organization(
        name=name,
        registration_number=f"REG-ADMFIN-{_seq:05d}",
        org_type="hospital",
        address="1 Admin Finance Way",
        city="Metro City",
        state="State",
        email=_next_email(),
        mobile="9876543210",
        password_hash="x",
        status=OrgStatus.APPROVED.value,
    )
    db_session.add(org)
    await db_session.commit()
    return org


async def _make_token_with_paid_payment(db_session, org) -> Payment:
    """A paid payment so an org has ledger-visible revenue activity."""
    counter = Counter(
        organization_id=org.id,
        counter_name=f"Admin Finance Desk {_seq}",
        counter_number=_seq % 100 or 1,
    )
    db_session.add(counter)
    await db_session.flush()
    service = Service(
        organization_id=org.id,
        name="Premium Registration",
        price=Decimal("500.00"),
    )
    db_session.add(service)
    await db_session.flush()
    token = Token(
        organization_id=org.id,
        counter_id=counter.id,
        service_id=service.id,
        token_number=f"ADMFIN-{_seq:04d}",
        sequence=1,
        status=TokenStatus.COMPLETED.value,
        amount_paid=Decimal("500.00"),
    )
    db_session.add(token)
    await db_session.flush()
    payment = Payment(
        organization_id=org.id,
        token_id=token.id,
        amount=Decimal("500.00"),
        refund_amount=Decimal("0.00"),
        currency="INR",
        payment_method=PaymentMethod.RAZORPAY.value,
        status=PaymentStatus.PAID.value,
        razorpay_payment_id=f"pay_admfin_{_seq}",
    )
    db_session.add(payment)
    await db_session.commit()
    await ledger_service.post_double_entry(
        db=db_session,
        organization_id=org.id,
        debit_account="gateway",
        credit_account="revenue",
        amount=payment.amount,
        currency="INR",
        entry_type=LedgerEntryType.CHARGE,
        payment_id=payment.id,
        source_transaction_ref=payment.razorpay_payment_id,
        description="Admin finance UI test charge",
    )
    await db_session.commit()
    return payment


async def _login_admin(client: AsyncClient, db_session) -> str:
    """Seed admin, login, return an admin access token (Bearer + cookie usable)."""
    await seed_default_admin(db_session)
    resp = await client.post("/auth/admin/login", json={
        "email": settings.admin_email,
        "password": settings.admin_password,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Page: login gate & rendering ──────────────────────────────────────────────

@pytest.mark.api
async def test_finance_page_requires_login(client: AsyncClient):
    resp = await client.get("/admin/finance", follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["location"]


@pytest.mark.api
async def test_finance_page_renders_payouts_and_overview(
    client: AsyncClient, db_session
):
    token = await _login_admin(client, db_session)
    org = await _make_org(db_session)
    payout = await payout_service.schedule_payout(
        db=db_session, organization_id=org.id, amount=Decimal("5000.00")
    )
    await _make_token_with_paid_payment(db_session, org)

    client.cookies.set("admin_token", token)
    resp = await client.get("/admin/finance")
    assert resp.status_code == 200, resp.text

    assert "Financial Management" in resp.text
    assert "Payout Review Queue" in resp.text          # payouts tab
    assert "Risk Flags" in resp.text                   # risk review tab
    assert "Bank Account Verification Queue" in resp.text  # FIN-002 tab
    assert "Ledger Overview" in resp.text              # LED-004 tab
    assert org.name in resp.text                       # org name shown for payout row
    assert f"#{payout.id}" in resp.text                # payout row present
    assert "Disburse" in resp.text                     # authorised action button
    assert "₹5000.00" in resp.text                     # payout amount rendering


@pytest.mark.api
async def test_finance_page_payout_status_filter(
    client: AsyncClient, db_session
):
    token = await _login_admin(client, db_session)
    org = await _make_org(db_session)
    scheduled = await payout_service.schedule_payout(
        db=db_session, organization_id=org.id, amount=Decimal("1200.00")
    )
    held_payout = await payout_service.schedule_payout(
        db=db_session, organization_id=org.id, amount=Decimal("3400.00")
    )
    held = await payout_service.hold_payout(
        db=db_session,
        payout_id=held_payout.id,
        reason="UI test hold",
        held_by_type="admin",
        held_by_id=1,
    )
    assert held.status == PayoutStatus.HELD.value

    client.cookies.set("admin_token", token)
    resp = await client.get("/admin/finance?status=held")
    assert resp.status_code == 200
    assert f"#{held_payout.id}" in resp.text
    assert "UI test hold" in resp.text
    assert "Release" in resp.text
    # the scheduled payout is filtered out of the held view
    assert f"#{scheduled.id}" not in resp.text

    resp_all = await client.get("/admin/finance")
    assert resp_all.status_code == 200
    assert f"#{held_payout.id}" in resp_all.text
    assert f"#{scheduled.id}" in resp_all.text


# ── FIN-001: hold / release / disburse through the admin APIs ─────────────────

@pytest.mark.api
async def test_payout_hold_release_disburse_flow(client: AsyncClient, db_session):
    admin_token = await _login_admin(client, db_session)
    org = await _make_org(db_session)
    payout = await payout_service.schedule_payout(
        db=db_session, organization_id=org.id, amount=Decimal("8000.00")
    )

    # Reason mandatory — schema-level 422 for missing/too-short reason
    short = await client.post(
        f"/admin/payouts/{payout.id}/hold", json={"reason": "x"}, headers=_bearer(admin_token)
    )
    assert short.status_code == 422

    # Disburse while scheduled is allowed only for scheduled payouts (guard)
    held = await client.post(
        f"/admin/payouts/{payout.id}/hold",
        json={"reason": "KYC document review pending"},
        headers=_bearer(admin_token),
    )
    assert held.status_code == 200, held.text
    assert held.json()["data"]["status"] == PayoutStatus.HELD.value
    assert held.json()["data"]["hold_reason"] == "KYC document review pending"

    released = await client.post(
        f"/admin/payouts/{payout.id}/release", headers=_bearer(admin_token)
    )
    assert released.status_code == 200
    data = released.json()["data"]
    assert data["status"] == PayoutStatus.SCHEDULED.value
    assert data["hold_reason"] is None

    disbursed = await client.post(
        f"/admin/payouts/{payout.id}/disburse", headers=_bearer(admin_token)
    )
    assert disbursed.status_code == 200
    assert disbursed.json()["data"]["status"] == PayoutStatus.PAID.value

    # LED-002: disbursement posts the immutable double-entry payout legs
    # (scoped to this org so the assertion is order-independent when other
    # modules have already disbursed payouts on the shared test database).
    entries = list((await db_session.execute(
        select(LedgerEntry).where(
            LedgerEntry.organization_id == org.id,
            LedgerEntry.entry_type == LedgerEntryType.PAYOUT.value,
        )
    )).scalars().all())
    assert len(entries) == 2  # one debit + one credit
    assert {e.direction for e in entries} == {"debit", "credit"}
    assert all(e.amount == Decimal("8000.00") for e in entries)


# ── FIN-003/004: risk flag review via admin API ───────────────────────────────

@pytest.mark.api
async def test_risk_flag_review_resolution(client: AsyncClient, db_session):
    admin_token = await _login_admin(client, db_session)
    org = await _make_org(db_session)
    flags = await financial_risk_service.evaluate_transaction_risk(
        db=db_session,
        organization_id=org.id,
        payment_id=None,
        amount=Decimal("25000.00"),
    )
    assert len(flags) == 1  # unusual_amount flag
    flag = flags[0]

    # Missing notes → schema validation error
    bad = await client.post(
        f"/admin/risk-flags/{flag.id}/resolve",
        json={"resolution_notes": ""},
        headers=_bearer(admin_token),
    )
    assert bad.status_code == 422

    resolved = await client.post(
        f"/admin/risk-flags/{flag.id}/resolve",
        json={
            "status": "resolved",
            "resolution_notes": "Authorized bulk purchase verified with the organization",
        },
        headers=_bearer(admin_token),
    )
    assert resolved.status_code == 200, resolved.text
    data = resolved.json()["data"]
    assert data["status"] == "resolved"
    assert data["resolved_by"] is not None
    assert data["resolution_notes"] == "Authorized bulk purchase verified with the organization"

    await db_session.refresh(flag)
    assert flag.status == "resolved"


# ── FIN-002: bank account listing + verification lift ─────────────────────────

@pytest.mark.api
async def test_bank_account_listing_and_verification(client: AsyncClient, db_session):
    admin_token = await _login_admin(client, db_session)
    org = await _make_org(db_session)
    account = await bank_account_service.update_bank_account(
        db=db_session,
        organization_id=org.id,
        account_number="123456789012",
        ifsc_code="HDFC0001234",
        account_holder_name="Finance Head",
        bank_name="HDFC Bank",
    )
    assert account.status == "pending_verification"

    listing = await client.get(
        "/admin/api/finance/bank-accounts", headers=_bearer(admin_token)
    )
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] >= 1
    ids = [a["organization_id"] for a in body["data"]]
    assert org.id in ids

    pending_only = await client.get(
        "/admin/api/finance/bank-accounts?status=pending_verification",
        headers=_bearer(admin_token),
    )
    assert org.id in [a["organization_id"] for a in pending_only.json()["data"]]

    verified = await client.post(
        f"/admin/bank-accounts/{org.id}/verify", headers=_bearer(admin_token)
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["data"]["status"] == "verified"
    assert verified.json()["data"]["held_until"] is None

    # Verification lifts the pending queue for this account
    after = await client.get(
        "/admin/api/finance/bank-accounts?status=pending_verification",
        headers=_bearer(admin_token),
    )
    assert account.id not in [a["id"] for a in after.json()["data"]]

    # Not registered organization → 400
    missing = await client.post(
        "/admin/bank-accounts/999999/verify", headers=_bearer(admin_token)
    )
    assert missing.status_code == 400


# ── LED-004: overview endpoint derives figures from ledger entries ────────────

@pytest.mark.api
async def test_finance_overview_endpoint(client: AsyncClient, db_session):
    admin_token = await _login_admin(client, db_session)
    org = await _make_org(db_session)
    await payout_service.schedule_payout(
        db=db_session, organization_id=org.id, amount=Decimal("1000.00")
    )
    await _make_token_with_paid_payment(db_session, org)

    resp = await client.get("/admin/api/finance/overview", headers=_bearer(admin_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["payout_counts"]["scheduled"] >= 1
    assert data["total_payouts"] >= 1
    assert data["flagged_risk_flags"] >= 0
    assert data["pending_bank_verifications"] >= 0

    org_rows = [r for r in data["organizations"] if r["organization_id"] == org.id]
    assert len(org_rows) == 1
    row = org_rows[0]
    assert row["organization_name"] == org.name
    assert float(row["gross_revenue"]) == 500.00       # ledger charge posted above
    assert float(row["net_revenue"]) == 500.00         # no refunds / fees on this data

    agg = data["aggregate_ledger_totals"]
    assert float(agg["gross_revenue"]) >= 500.00
    assert {"gross_revenue", "cash_collected", "gateway_collected", "refunds",
            "platform_fee", "net_revenue"} <= set(agg.keys())

    # Unauthenticated access rejected (router-level admin dependency).
    # /auth/admin/login sets the admin_token cookie in the client jar
    # (normal browser-session behavior), so clear it to simulate a true
    # anonymous request.
    client.cookies.clear()
    anon = await client.get("/admin/api/finance/overview")
    assert anon.status_code in (401, 403)
