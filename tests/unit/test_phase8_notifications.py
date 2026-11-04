"""
tests/unit/test_phase8_notifications.py
───────────────────────────────────────
Phase 8 — Reliable notification delivery (NTF-001..005):

- durable outbox: pending rows for configured providers, honest
  "provider unconfigured" SUPPRESSED state, dedup within the window,
  retry/backoff with attempts, FAILED never marked sent;
- expiry-safe queue-call suppression (token past the callable stage);
- per-user preferences with untouchable essential types (NTF-004);
- sensitive-detail minimisation in subjects/SMS (NTF-003);
- in-app dedup keys (NTF-005).

conftest patches `aiosmtplib.send` to succeed by default; failure paths
monkeypatch `notification_service.send_email` directly.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.services import notification_service as ns
from models.notification import (
    ESSENTIAL_TYPES,
    Notification,
    NotificationType,
)
from models.notification_outbox import NotificationOutbox, OutboxStatus
from models.token import TokenStatus
from tests.phase7_helpers import make_counter, make_org, make_token, make_user


def _mail_ready(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "mail_username", "mailer@example.com")


async def _queue_email(db_session, monkeypatch, **kwargs):
    _mail_ready(monkeypatch)
    return await ns.queue_outbox(
        db_session, channel="email", recipient="u@example.com",
        subject="Called", body="<p>called</p>",
        notification_type="token_called", **kwargs,
    )


class TestQueueOutbox:
    async def test_pending_when_provider_configured(self, db_session, monkeypatch):
        row = await _queue_email(db_session, monkeypatch)
        assert row is not None
        assert row.status == OutboxStatus.PENDING
        assert row.last_error is None
        assert row.attempts == 0

    async def test_suppressed_when_provider_unconfigured(self, db_session, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "mail_username", "")
        row = await ns.queue_outbox(
            db_session, channel="email", recipient="u@example.com",
            subject="Hi", body="<p>hello</p>",
        )
        # honest gap recording — a failed delivery is never dressed as sent
        assert row.status == OutboxStatus.SUPPRESSED
        assert row.last_error == "provider unconfigured"

    async def test_sms_suppressed_when_twilio_unconfigured(self, db_session, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "twilio_account_sid", "")
        row = await ns.queue_outbox(
            db_session, channel="sms", recipient="9800000000",
            body="Your token has been called",
            notification_type="token_called",
        )
        assert row.status == OutboxStatus.SUPPRESSED
        assert row.last_error == "provider unconfigured"

    async def test_dedup_key_collapses_within_window(self, db_session, monkeypatch):
        first = await _queue_email(db_session, monkeypatch, dedup_key="qcall:42")
        second = await ns.queue_outbox(
            db_session, channel="email", recipient="u@example.com",
            subject="Called again", body="<p>called again</p>",
            dedup_key="qcall:42",
        )
        assert first is not None
        assert second is None

    async def test_subject_minimised_on_queue(self, db_session, monkeypatch):
        from app.config import settings
        _mail_ready(monkeypatch)
        row = await ns.queue_outbox(
            db_session, channel="email", recipient="someone@example.com",
            subject="Receipt 9876543210 for user bob@bank.com",
            body="ok",
        )
        for secret in ("9876543210", "bob@bank.com"):
            assert secret not in row.subject
        assert len(row.subject) <= ns.SUBJECT_MAX_LENGTH


class TestDispatch:
    async def test_delivered_sent_and_never_back_to_pending(self, db_session, mock_smtp_and_cleanup_tasks, monkeypatch):
        row = await _queue_email(db_session, monkeypatch)
        await ns.dispatch_outbox_row(row.id, session=db_session)
        await db_session.refresh(row)
        assert row.status == OutboxStatus.SENT
        assert row.attempts == 1
        assert row.sent_at is not None
        assert row.last_error is None

    async def test_failure_records_attempts_and_never_becomes_sent(self, db_session, monkeypatch):
        failed_send = AsyncMock(return_value=False)
        monkeypatch.setattr(ns, "send_email", failed_send)
        row = await _queue_email(db_session, monkeypatch)
        for _ in range(row.max_attempts):
            await ns.dispatch_outbox_row(row.id, session=db_session)
            await db_session.refresh(row)
            if row.status == OutboxStatus.FAILED:
                break
        assert row.status == OutboxStatus.FAILED
        assert row.attempts == row.max_attempts
        assert row.sent_at is None  # NEVER claimed delivered
        assert "failed" in row.last_error

    async def test_backoff_scheduled_between_attempts(self, db_session, monkeypatch):
        failed_send = AsyncMock(return_value=False)
        monkeypatch.setattr(ns, "send_email", failed_send)
        row = await _queue_email(db_session, monkeypatch)
        await ns.dispatch_outbox_row(row.id, session=db_session)
        await db_session.refresh(row)
        assert row.status == OutboxStatus.PENDING
        assert row.attempts == 1
        assert row.next_attempt_at is not None
        assert row.next_attempt_at > row.first_attempt_at
        # NTF-002: backoff is exponential over the attempts
        assert (row.next_attempt_at - row.first_attempt_at).total_seconds() >= 30

    async def test_expiry_safe_token_no_longer_callable(self, db_session, monkeypatch):
        org = await make_org(db_session)
        counter = await make_counter(db_session, org)
        token = await make_token(db_session, org, counter, status=TokenStatus.WAITING)
        _mail_ready(monkeypatch)
        row = await ns.queue_outbox(
            db_session, channel="email", recipient="u@example.com",
            subject="Called", body="x", token_id=token.id,
            dedup_key="qcall:expiry-test",
        )
        # the token advanced past the callable stage before delivery ran
        token.status = TokenStatus.COMPLETED
        await db_session.flush()
        await ns.dispatch_outbox_row(row.id, session=db_session)
        await db_session.refresh(row)
        assert row.status == OutboxStatus.SUPPRESSED
        assert row.last_error == "token no longer callable"

    async def test_missing_row_is_ignored(self, db_session):
        # no crash for an outbox id that does not exist
        await ns.dispatch_outbox_row(999999, session=db_session)

    async def test_dispatched_row_is_not_re_dispatched(self, db_session, mock_smtp_and_cleanup_tasks, monkeypatch):
        row = await _queue_email(db_session, monkeypatch)
        await ns.dispatch_outbox_row(row.id, session=db_session)
        # second dispatch attempt must not re-send or reset state
        await ns.dispatch_outbox_row(row.id, session=db_session)
        await db_session.refresh(row)
        assert row.status == OutboxStatus.SENT
        assert row.attempts == 1


class TestPreferences:
    async def test_optional_type_can_be_disabled(self, db_session):
        user = await make_user(db_session)
        prefs = await ns.set_preferences(db_session, user.id, ["token_created"])
        assert prefs.disabled_types == ["token_created"]
        fetched = await ns.get_preferences(db_session, user.id)
        assert fetched.id == prefs.id
        assert ns.type_allowed(fetched, NotificationType.TOKEN_CREATED) is False

    async def test_essential_types_cannot_be_disabled(self, db_session):
        user = await make_user(db_session)
        for essential in sorted(ESSENTIAL_TYPES):
            with pytest.raises(ValueError, match="essential"):
                await ns.set_preferences(db_session, user.id, [essential])

    async def test_unknown_type_rejected(self, db_session):
        user = await make_user(db_session)
        with pytest.raises(ValueError, match="Unknown notification type"):
            await ns.set_preferences(db_session, user.id, ["carrier_pigeon"])

    async def test_essential_allowed_even_if_user_tried_to_disable(self, db_session):
        user = await make_user(db_session)
        prefs = await ns.set_preferences(db_session, user.id, ["token_created"])
        assert ns.type_allowed(prefs, NotificationType.SECURITY_NOTICE) is True

    async def test_no_preferences_row_means_everything_allowed(self, db_session):
        user = await make_user(db_session)
        assert ns.type_allowed(None, NotificationType.QUEUE_NEARING) is True


class TestInAppDedup:
    async def test_same_dedup_key_collapses(self, db_session):
        user = await make_user(db_session)
        first = await ns.create_in_app(
            db_session, user.id, NotificationType.TOKEN_CALLED,
            "called once", dedup_key="inapp:qcall:77",
        )
        second = await ns.create_in_app(
            db_session, user.id, NotificationType.TOKEN_CALLED,
            "called twice", dedup_key="inapp:qcall:77",
        )
        assert first is not None
        assert second is None
        rows = (await db_session.execute(
            select(Notification).where(
                Notification.user_id == user.id,
                Notification.dedup_key == "inapp:qcall:77",
            )
        )).scalars().all()
        assert len(rows) == 1

    async def test_in_app_rows_carry_delivered_template_fields(self, db_session):
        user = await make_user(db_session)
        notif = await ns.create_in_app(
            db_session, user.id, NotificationType.TOKEN_CREATED, "booked",
        )
        assert notif.delivery_state == "delivered"
        assert notif.template_version == ns.TEMPLATE_VERSION


class TestMinimization:
    def test_mobile_email_and_digit_runs_redacted(self):
        text = ns.minimal_preview_text(
            "Token A-001 for +91 98765 43210 (bob@corp.com), ref 1234567890"
        )
        assert "98765 43210" not in text
        assert "9876543210" not in text.replace(" ", "")
        assert "bob@corp.com" not in text
        assert "1234567890" not in text
        assert "Token A-001" in text

    def test_subject_length_cap(self):
        subject = ns.minimal_preview_text(
            "[Skip Line] " + "x" * 500, ns.SUBJECT_MAX_LENGTH
        )
        assert len(subject) <= ns.SUBJECT_MAX_LENGTH
        assert subject.endswith("…")


class TestSendGuards:
    async def test_send_email_false_when_unconfigured(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "mail_username", "")
        assert await ns.send_email("u@example.com", "Subject", "<p>Hi</p>") is False

    async def test_send_sms_false_when_unconfigured(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "twilio_account_sid", "")
        assert await ns.send_sms("9800000000", "hi") is False
