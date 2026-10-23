"""tests/unit/test_notification_service.py"""

import pytest
from models.notification import NotificationChannel, NotificationType


class TestNotificationEnums:
    def test_all_channels(self):
        assert set(NotificationChannel) == {
            NotificationChannel.EMAIL,
            NotificationChannel.SMS,
            NotificationChannel.IN_APP,
        }

    def test_all_types(self):
        types = {t.value for t in NotificationType}
        assert "token_created"  in types
        assert "token_called"   in types
        assert "queue_nearing"  in types
        assert "token_completed" in types
        assert "payment_success" in types

    def test_channel_values(self):
        assert NotificationChannel.EMAIL.value  == "email"
        assert NotificationChannel.SMS.value    == "sms"
        assert NotificationChannel.IN_APP.value == "in_app"


import asyncio
from unittest.mock import AsyncMock, patch

from app.services.notification_service import (
    _background_tasks,
    _track_background_task,
    wait_for_background_tasks,
    send_email,
    notify_token_created,
    notify_token_called,
)
from models.token import Token
from models.user import User


class TestBackgroundTaskTracking:
    async def test_track_background_task_success(self):
        async def dummy_coro():
            await asyncio.sleep(0.01)
            return "ok"

        task = _track_background_task(dummy_coro(), name="test_dummy")
        assert task in _background_tasks
        assert task.get_name() == "test_dummy"
        await task
        assert task not in _background_tasks

    async def test_track_background_task_exception_handled(self):
        async def failing_coro():
            await asyncio.sleep(0.01)
            raise ValueError("Intentional failure")

        task = _track_background_task(failing_coro(), name="test_fail")
        assert task in _background_tasks
        try:
            await task
        except ValueError:
            pass
        assert task not in _background_tasks
        assert task.done()
        assert isinstance(task.exception(), ValueError)

    async def test_wait_for_background_tasks_drains_tasks(self):
        async def short_coro():
            await asyncio.sleep(0.02)

        t1 = _track_background_task(short_coro(), name="drain_1")
        t2 = _track_background_task(short_coro(), name="drain_2")
        assert len(_background_tasks) >= 2
        await wait_for_background_tasks(timeout=1.0)
        assert t1 not in _background_tasks
        assert t2 not in _background_tasks

    async def test_wait_for_background_tasks_timeout_cancels(self):
        async def infinite_coro():
            await asyncio.sleep(100.0)

        t = _track_background_task(infinite_coro(), name="infinite")
        await wait_for_background_tasks(timeout=0.05)
        assert t.cancelled()
        assert t not in _background_tasks


class TestSendEmail:
    async def test_send_email_skips_when_no_username(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "mail_username", "")
        with patch("aiosmtplib.send") as mock_send:
            await send_email("user@example.com", "Subject", "<p>Hello</p>")
            mock_send.assert_not_called()

    async def test_send_email_calls_smtp_when_configured(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "mail_username", "mailer@example.com")
        mock_send = AsyncMock(return_value=(None, "250 OK"))
        monkeypatch.setattr("aiosmtplib.send", mock_send)

        await send_email("user@example.com", "Test Subject", "<p>Body</p>")
        mock_send.assert_called_once()
        sent_msg = mock_send.call_args[0][0]
        assert sent_msg["Subject"] == "Test Subject"
        assert sent_msg["To"] == "user@example.com"


class TestNotificationDispatch:
    async def test_notify_token_created_dispatches_email(self, db_session):
        from models.token import TokenStatus
        user = User(name="Test User", email="user@example.com", mobile="9000000000", password_hash="hash")
        db_session.add(user)
        await db_session.flush()

        token = Token(
            token_number="A-001",
            sequence=1,
            queue_position=1,
            organization_id=1,
            counter_id=1,
            user_id=user.id,
            status=TokenStatus.WAITING,
        )
        db_session.add(token)
        await db_session.flush()

        with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
            await notify_token_created(db_session, token, user)
            await wait_for_background_tasks(timeout=1.0)
            mock_send.assert_called_once()
