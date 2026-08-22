"""tests/unit/test_config.py — Settings and configuration tests."""

import pytest
from app.config import settings


class TestSettings:
    def test_app_name_set(self):
        assert settings.app_name == "Skip Line"

    def test_jwt_algorithm_valid(self):
        assert settings.jwt_algorithm in ("HS256", "RS256", "HS512")

    def test_async_database_url_format(self):
        url = settings.async_database_url
        assert url.startswith("mysql+aiomysql://")
        assert settings.db_name in url

    def test_sync_database_url_format(self):
        url = settings.sync_database_url
        assert url.startswith("mysql+pymysql://")

    def test_qr_dir_set(self):
        assert settings.qr_code_dir
        assert isinstance(settings.qr_code_dir, str)

    def test_receipt_dir_set(self):
        assert settings.receipt_dir
        assert isinstance(settings.receipt_dir, str)

    def test_is_production_flag(self):
        # In test env, APP_ENV is not "production"
        assert settings.is_production is False

    def test_token_expire_minutes_positive(self):
        assert settings.jwt_access_token_expire_minutes > 0

    def test_cors_origins_is_list(self):
        assert isinstance(settings.cors_origins, list)
        assert len(settings.cors_origins) >= 1
