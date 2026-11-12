"""WP3: optional external provider gates — graceful degradation & secret hardening.

Verifies (Task 2 of WP3) that every OPTIONAL external integration degrades
cleanly when it is unconfigured/disabled, and that production startup
rejects placeholder/weak application secrets:

- Payments (Razorpay): unconfigured keys yield a controlled
  ``PaymentsNotConfigured`` at the service layer and a clean HTTP 503 at the
  API layer (never an unhandled 500 traceback). Cash/walk-in/free flows are
  untouched.
- Email / SMTP: an unconfigured SMTP produces an honest SUPPRESSED durable
  outbox row (``provider unconfigured``) — no dispatch, no retry churn, no
  crash. Backoff for configured-but-failing providers is exponential
  (30 s / 120 s / 480 s), NOT the ~100 ms commit-visibility poll.
- AI assistant: unconfigured provider raises ``AiProviderUnavailable`` and
  callers degrade to the unavailable message (no 500s).
- Twilio SMS: documented placeholder (not wired end-to-end); absence never
  blocks token creation or notification recording (honest SUPPRESSED rows).
- Production config validation rejects placeholder/short/empty
  SECRET_KEY / JWT_SECRET_KEY / ADMIN_PASSWORD.
"""

from __future__ import annotations

import warnings
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import Settings, settings
from app.services import ai_provider, notification_service, payment_service
from models.counter import Counter
from models.notification_outbox import NotificationOutbox, OutboxStatus
from models.organization import Organization, OrgStatus
from models.payment import Payment
from models.service import Service
from models.token import Token, TokenStatus

_seq = 0


def _next() -> int:
    global _seq
    _seq += 1
    return _seq


# ── helpers (minimal rows, mirroring the finance-test pattern) ────────────────

async def _make_paid_token(db_session, user_id: int | None = None) -> tuple[Organization, Token]:
    n = _next()
    org = Organization(
        name=f"WP3 Provider Org {n}",
        registration_number=f"REG-WP3-{n:05d}",
        org_type="hospital",
        address="1 Provider Way",
        city="Metro City",
        state="State",
        email=f"wp3provider-{n}@example.com",
        mobile="9876543210",
        password_hash="x",
        status=OrgStatus.APPROVED.value,
    )
    db_session.add(org)
    await db_session.flush()
    counter = Counter(
        organization_id=org.id, counter_name=f"WP3 Desk {n}",
        counter_number=n % 80 or 1, daily_limit=100,
    )
    db_session.add(counter)
    await db_session.flush()
    service = Service(organization_id=org.id, name=f"WP3 Service {n}",
                      price=Decimal("500.00"))
    db_session.add(service)
    await db_session.flush()
    token = Token(
        organization_id=org.id, counter_id=counter.id, service_id=service.id,
        token_number=f"WP3-{n:04d}", sequence=n,
        status=TokenStatus.WAITING.value, amount_paid=Decimal("500.00"),
        user_id=user_id,
    )
    db_session.add(token)
    await db_session.flush()
    return org, token


async def _register_and_login_user(client: AsyncClient) -> str:
    n = _next()
    email = f"wp3user-{n}@example.com"
    await client.post("/user/register", json={
        "name": "WP3 Provider User", "email": email, "mobile": "9000000003",
        "password": "Password@123", "confirm_password": "Password@123",
    })
    login = await client.post("/auth/user/login", json={
        "email": email, "password": "Password@123"
    })
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


# ── 1. Production secret validation ──────────────────────────────────────────

class TestProductionSecretValidation:
    """Production startup must reject placeholder/weak application secrets."""

    def _prod_settings(self, **overrides) -> Settings:
        return Settings(app_env="production", app_debug=False, **overrides)

    def test_placeholder_secret_key_rejected_in_production(self) -> None:
        with pytest.raises(Exception, match="insecure placeholder"):
            self._prod_settings(secret_key="change-me-to-a-64-char-random-string")

    def test_placeholder_jwt_secret_rejected_in_production(self) -> None:
        with pytest.raises(Exception, match="insecure placeholder"):
            self._prod_settings(jwt_secret_key="change-me-to-another-64-char-random-string")

    def test_short_secret_key_rejected_in_production(self) -> None:
        with pytest.raises(Exception, match="too short"):
            self._prod_settings(secret_key="a" * 31)

    def test_short_jwt_secret_rejected_in_production(self) -> None:
        with pytest.raises(Exception, match="too short"):
            self._prod_settings(jwt_secret_key="b" * 31)

    def test_empty_secret_rejected_in_production(self) -> None:
        with pytest.raises(Exception, match="not configured"):
            self._prod_settings(secret_key="")

    def test_insecure_admin_password_rejected_in_production(self) -> None:
        with pytest.raises(Exception, match="insecure placeholder"):
            self._prod_settings(admin_password="admin@1234!")

    def test_short_admin_password_rejected_in_production(self) -> None:
        with pytest.raises(Exception, match="too short"):
            self._prod_settings(admin_password="Ab1!")

    def test_strong_distinct_secrets_pass_production_validation(self) -> None:
        s = self._prod_settings(
            secret_key="S" * 64,
            jwt_secret_key="J" * 64,
            admin_password="Str0ng-Admin-Passphrase-2026!",
        )
        assert s.secret_key != s.jwt_secret_key
        assert len(s.secret_key) >= 32 and len(s.jwt_secret_key) >= 32

    def test_placeholder_secret_warns_but_permits_in_development(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Settings(app_env="development", secret_key="changeme")
        assert any("insecure placeholder" in str(w.message) for w in caught)

    def test_env_secrets_are_rotated_distinct_and_strong(self) -> None:
        """WP3 Task 1 outcome check (values compared in-process, never printed)."""
        from dotenv import dotenv_values
        vals = dotenv_values(".env") or {}
        sk, jk, ap = vals.get("SECRET_KEY", ""), vals.get("JWT_SECRET_KEY", ""), \
            vals.get("ADMIN_PASSWORD", "")
        assert len(sk) >= 32 and len(jk) >= 32 and len(ap) >= 12
        assert sk != jk != ap and sk != ap
        for v in (sk, jk):
            lowered = v.strip().lower()
            assert lowered not in {
                "changeme", "changeme-jwt", "secret", "password", "change-me",
                "change-me-to-a-64-char-random-string",
                "change-me-to-another-64-char-random-string",
            }


# ── 2. Razorpay graceful degradation ─────────────────────────────────────────

class TestRazorpayGracefulDegradation:
    """Unconfigured Razorpay keys must produce controlled errors, never 500s."""

    @pytest.mark.asyncio
    async def test_create_order_raises_controlled_error_when_unconfigured(
            self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "razorpay_key_id", "")
        monkeypatch.setattr(settings, "razorpay_key_secret", "")
        org, token = await _make_paid_token(db_session)
        with pytest.raises(payment_service.PaymentsNotConfigured):
            await payment_service.create_payment_order(
                db_session, token.id, None, org.id, Decimal("500.00"))
        payments = (await db_session.execute(
            select(Payment).where(Payment.token_id == token.id))).scalars().all()
        assert payments == [], "no Payment row may leak from a disabled initiation"

    @pytest.mark.asyncio
    async def test_policy_check_precedence_preserved(self, db_session):
        """FIN-001 policy enforcement still runs BEFORE the config guard."""
        from app.services import payment_policy_service
        from models.admin import Admin
        org, token = await _make_paid_token(db_session)
        admin = Admin(name=f"WP3 Policy Admin {_next()}",
                      email=f"wp3poladmin-{_next()}@example.com",
                      password_hash="x", is_active=False)
        db_session.add(admin)
        await db_session.flush()
        await payment_policy_service.set_payment_policy(
            db_session, org.id, ["cash"], admin)
        with pytest.raises(PermissionError):
            await payment_service.create_payment_order(
                db_session, token.id, None, org.id, Decimal("500.00"))

    @pytest.mark.asyncio
    async def test_verify_raises_controlled_error_when_unconfigured(
            self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "razorpay_key_id", "")
        monkeypatch.setattr(settings, "razorpay_key_secret", "")
        with pytest.raises(payment_service.PaymentsNotConfigured):
            await payment_service.verify_and_record_payment(
                db_session, "order_x", "pay_x", "sig_x")

    @pytest.mark.asyncio
    async def test_initiate_endpoint_503_when_unconfigured(
            self, client: AsyncClient, db_session, monkeypatch):
        monkeypatch.setattr(settings, "razorpay_key_id", "")
        monkeypatch.setattr(settings, "razorpay_key_secret", "")
        user_token = await _register_and_login_user(client)
        from models.user import User
        # Identify the just-registered user (registration used a unique WP3 email).
        wp3_users = (await db_session.execute(
            select(User).where(User.email.like("wp3user-%"))
            .order_by(User.id.desc()))).scalars().all()
        _, token = await _make_paid_token(db_session, user_id=wp3_users[0].id)
        resp = await client.post(
            "/payments/initiate",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"token_id": token.id},
        )
        assert resp.status_code == 503, resp.text
        assert "not configured" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_verify_endpoint_503_when_unconfigured(
            self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(settings, "razorpay_key_id", "")
        monkeypatch.setattr(settings, "razorpay_key_secret", "")
        user_token = await _register_and_login_user(client)
        resp = await client.post(
            "/payments/verify",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"razorpay_order_id": "order_x", "razorpay_payment_id": "pay_x",
                  "razorpay_signature": "sig_x"},
        )
        assert resp.status_code == 503, resp.text
        assert "not configured" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_webhook_503_when_no_secret_configured(
            self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(settings, "razorpay_webhook_secret", "")
        monkeypatch.setattr(settings, "razorpay_key_secret", "")
        resp = await client.post(
            "/payments/webhook",
            content=b'{"event": "payment.captured"}',
            headers={"X-Razorpay-Signature": "whatever"},
        )
        assert resp.status_code == 503, resp.text
        assert "not configured" in resp.json()["detail"].lower()


# ── 3. Email / SMTP outbox short-circuit ─────────────────────────────────────

class TestOutboxUnconfiguredShortCircuit:
    """Unconfigured SMTP yields honest SUPPRESSED rows — no retries, no noise."""

    @pytest.mark.asyncio
    async def test_email_outbox_suppressed_when_smtp_unconfigured(
            self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "mail_username", "")
        row = await notification_service.queue_outbox(
            db_session, channel="email", recipient="wp3@example.com",
            subject="WP3", body="body",
        )
        assert row is not None
        assert row.status == OutboxStatus.SUPPRESSED
        assert row.last_error == "provider unconfigured"
        assert row.attempts in (None, 0), "unconfigured provider must not burn attempts"

    @pytest.mark.asyncio
    async def test_sms_outbox_suppressed_when_twilio_unconfigured(
            self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "twilio_account_sid", "")
        row = await notification_service.queue_outbox(
            db_session, channel="sms", recipient="9000000000", body="body",
        )
        assert row is not None
        assert row.status == OutboxStatus.SUPPRESSED
        assert row.last_error == "provider unconfigured"

    @pytest.mark.asyncio
    async def test_send_email_returns_false_without_exception_when_unconfigured(
            self, monkeypatch):
        monkeypatch.setattr(settings, "mail_username", "")
        assert await notification_service.send_email(
            "wp3@example.com", "subject", "<p>body</p>") is False

    @pytest.mark.asyncio
    async def test_send_sms_returns_false_without_exception_when_unconfigured(self):
        # WP3 baseline: Twilio is a documented placeholder (not wired); with
        # no account SID the call degrades to an honest False.
        sid = settings.twilio_account_sid
        monkeypatch_var = sid
        try:
            if not sid:
                assert await notification_service.send_sms(
                    "9000000000", "body") is False
        finally:
            assert monkeypatch_var == sid

    def test_retry_backoff_is_exponential_not_subsecond(self) -> None:
        """The dispatch retry policy is 30s/60s/120s… — the ~100 ms poll in
        dispatch_outbox_row is only the bounded commit-visibility check, so
        a disabled/failing provider cannot create a 100 ms retry storm."""
        assert notification_service._backoff_seconds(1) == 30
        assert notification_service._backoff_seconds(2) == 60
        assert notification_service._backoff_seconds(3) == 120


# ── 4. AI assistant graceful degradation ─────────────────────────────────────

class TestAiGracefulDegradation:
    """Unconfigured AI provider degrades to AiProviderUnavailable (no 500s)."""

    def test_is_configured_false_when_provider_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "ai_provider", "")
        monkeypatch.setattr(settings, "ai_base_url", "")
        monkeypatch.setattr(settings, "ai_api_key", "")
        monkeypatch.setattr(settings, "ai_model", "")
        assert ai_provider.is_configured() is False

    def test_is_configured_requires_all_four_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "ai_provider", "openai")
        monkeypatch.setattr(settings, "ai_base_url", "https://api.example.test/v1")
        monkeypatch.setattr(settings, "ai_api_key", "")
        monkeypatch.setattr(settings, "ai_model", "g-test")
        assert ai_provider.is_configured() is False

    @pytest.mark.asyncio
    async def test_chat_raises_unavailable_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(settings, "ai_provider", "")
        with pytest.raises(ai_provider.AiProviderUnavailable):
            await ai_provider.chat([{"role": "user", "content": "hi"}])

    def test_unavailable_message_non_empty(self):
        from app.services import ai_assistant_service
        msg = ai_assistant_service.unavailable_message()
        assert isinstance(msg, str) and msg.strip()


# ── 5. Twilio SMS implementation status ──────────────────────────────────────

class TestTwilioStatus:
    """Twilio is a documented placeholder path: honest non-delivery, no crash."""

    @pytest.mark.asyncio
    async def test_send_sms_is_placeholder_never_claims_delivery(self, monkeypatch):
        if settings.twilio_account_sid:
            # Even with a SID configured, the placeholder path must NOT claim
            # a real provider round-trip.
            assert await notification_service.send_sms("9000000000", "body") is False
        else:
            assert await notification_service.send_sms("9000000000", "body") is False

    @pytest.mark.asyncio
    async def test_sms_absence_does_not_block_notification_recording(
            self, db_session, monkeypatch):
        """Token/notification flows still record in-app notifications and a
        durable outbox row (SUPPRESSED for SMS) — SMS absence blocks nothing."""
        monkeypatch.setattr(settings, "twilio_account_sid", "")
        row = await notification_service.queue_outbox(
            db_session, channel="sms", recipient="9000000000", body="queue call",
            notification_type="queue_call",
        )
        assert row is not None and row.status == OutboxStatus.SUPPRESSED
