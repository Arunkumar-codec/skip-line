"""
app/config.py
─────────────
Central configuration loaded from environment variables / .env file.
All other modules import `settings` from here — never os.environ directly.
"""

from functools import lru_cache
from typing import List
from urllib.parse import quote_plus 
from pydantic import AnyUrl, EmailStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────────
    app_name: str = "Skip Line"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = True
    secret_key: str = "changeme"
    allowed_hosts: List[str] = ["localhost", "127.0.0.1"]

    # ── JWT ──────────────────────────────────────────────────────────────────
    jwt_secret_key: str = "changeme-jwt"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 7

    # ── Database ─────────────────────────────────────────────────────────────
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "skipline"
    db_user: str = "skipline_user"
    db_password: str = "***REMOVED***"

    @property
    def async_database_url(self) -> str:
         return (
             f"mysql+aiomysql://{self.db_user}:"
             f"{quote_plus(self.db_password)}@"
             f"{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def sync_database_url(self) -> str:
        """Used by Alembic (synchronous)."""
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ── Email ────────────────────────────────────────────────────────────────
    mail_username: str = ""
    mail_password: str = ""
    mail_from: str = "noreply@skipline.app"
    mail_from_name: str = "Skip Line"
    mail_server: str = "smtp.gmail.com"
    mail_port: int = 587
    mail_starttls: bool = True
    mail_ssl_tls: bool = False

    # ── SMS ──────────────────────────────────────────────────────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # ── Payment ──────────────────────────────────────────────────────────────
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_currency: str = "INR"
    razorpay_webhook_secret: str = ""

    # ── File paths ───────────────────────────────────────────────────────────
    qr_code_dir: str = "qr_codes"
    receipt_dir: str = "receipts"
    static_dir: str = "static"

    # ── CORS ─────────────────────────────────────────────────────────────────
    cors_origins: List[str] = ["http://localhost:8000"]

    # ── Admin bootstrap ──────────────────────────────────────────────────────
    admin_name: str = "Super Admin"
    admin_email: str = "admin@skipline.app"
    admin_password: str = "Admin@1234!"

    # ── Derived helpers ──────────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @field_validator("allowed_hosts", "cors_origins", mode="before")
    @classmethod
    def split_comma(cls, v):
        if isinstance(v, str):
            return [h.strip() for h in v.split(",")]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
