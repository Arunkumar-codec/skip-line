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
