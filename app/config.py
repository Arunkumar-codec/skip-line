"""
app/config.py
─────────────
Central configuration loaded from environment variables / .env file.
All other modules import `settings` from here — never os.environ directly.
"""

import secrets
import warnings
from functools import lru_cache
from typing import List
from urllib.parse import quote_plus
from pydantic import AnyUrl, EmailStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Known-insecure values that production must reject outright (case-insensitive).
# The first two are the removed hardcoded defaults; the rest are common
# placeholder secrets.
_INSECURE_SECRET_VALUES = frozenset(
    {"changeme", "changeme-jwt", "secret", "password"}
)
_INSECURE_ADMIN_PASSWORDS = frozenset(
    {"admin", "password", "changeme", "admin@1234!"}
)
# Strong required lengths for production.
_MIN_GLOBAL_SECRET_LENGTH = 32
_MIN_ADMIN_PASSWORD_LENGTH = 12


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
    secret_key: str = ""
    allowed_hosts: List[str] = ["localhost", "127.0.0.1"]

    # ── JWT ──────────────────────────────────────────────────────────────────
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 7

    # ── Database ─────────────────────────────────────────────────────────────
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "skipline"
    db_user: str = "skipline_user"
    db_password: str = ""

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
            f"mysql+pymysql://{self.db_user}:"
            f"{quote_plus(self.db_password)}"
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
    upload_dir: str = "uploads"
    identity_dir: str = "uploads/identities"

    # ── CORS ─────────────────────────────────────────────────────────────────
    cors_origins: List[str] = ["http://localhost:8000"]

    # ── Admin bootstrap ──────────────────────────────────────────────────────
    admin_name: str = "Super Admin"
    admin_email: str = "admin@skipline.app"
    admin_password: str = ""

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

    @model_validator(mode="after")
    def validate_db_password(self) -> "Settings":
        """Fail clearly when the database credential is not configured.

        The password must come from the DB_PASSWORD environment variable (or
        the .env file). No insecure default is substituted: an empty password
        simply means database connections cannot succeed.

        - production: a missing credential is a hard configuration error.
        - other environments: allowed (e.g. SQLite-backed tests), but a
          warning is emitted so local misconfiguration is visible.
        """
        if not self.db_password:
            message = (
                "DB_PASSWORD is not configured. Set the DB_PASSWORD "
                "environment variable (see .env.example) to provide the "
                "database password."
            )
            if self.app_env == "production":
                raise ValueError(message)
            warnings.warn(
                f"{message} Database connections will fail until it is set.",
                RuntimeWarning,
                stacklevel=2,
            )
        return self

    def _validate_global_secret(self, name: str, production: bool) -> None:
        """Validate one signing secret (secret_key / jwt_secret_key).

        Missing in production is a hard error; missing elsewhere gets a secure
        ephemeral random value generated for this process (a warning makes the
        ephemerality visible — e.g. tokens/identity-encryption keys do not
        survive restarts until a stable value is set in .env). Known-insecure
        values are rejected in production and warned about elsewhere.
        """
        env_name = name.upper()
        value = getattr(self, name)
        missing_message = (
            f"{env_name} is not configured. Set the {env_name} "
            "environment variable (see .env.example)."
        )
        if not value:
            if production:
                raise ValueError(missing_message)
            warnings.warn(
                f"{missing_message} A secure random value was generated for "
                "this process only and is regenerated on every restart — "
                "set a stable value in .env for persistent behavior.",
                RuntimeWarning,
                stacklevel=3,
            )
            setattr(self, name, secrets.token_urlsafe(48))
        elif value.strip().lower() in _INSECURE_SECRET_VALUES:
            insecure_message = (
                f"{env_name} uses a known insecure placeholder value. "
                f"Provide a strong random secret of at least "
                f"{_MIN_GLOBAL_SECRET_LENGTH} characters (see .env.example)."
            )
            if production:
                raise ValueError(insecure_message)
            warnings.warn(
                f"{insecure_message} Permitted locally; production rejects it.",
                RuntimeWarning,
                stacklevel=3,
            )
        elif production and len(value) < _MIN_GLOBAL_SECRET_LENGTH:
            raise ValueError(
                f"{env_name} is too short for production use: provide at "
                f"least {_MIN_GLOBAL_SECRET_LENGTH} characters."
            )

    @model_validator(mode="after")
    def validate_secrets(self) -> "Settings":
        """Enforce secure signing secrets and the admin bootstrap password.

        Production fails clearly at startup when a required secret is absent
        or insecure; no insecure default is ever substituted.
        """
        production = self.app_env == "production"
        for name in ("secret_key", "jwt_secret_key"):
            self._validate_global_secret(name, production)
        if not self.admin_password:
            message = (
                "ADMIN_PASSWORD is not configured. Set the ADMIN_PASSWORD "
                "environment variable (see .env.example)."
            )
            if production:
                raise ValueError(message)
            warnings.warn(
                f"{message} The default Super Admin account is NOT seeded "
                "until it is set.",
                RuntimeWarning,
                stacklevel=2,
            )
        else:
            lowered = self.admin_password.strip().lower()
            if lowered in _INSECURE_ADMIN_PASSWORDS:
                insecure_message = (
                    "ADMIN_PASSWORD uses a known insecure placeholder value; "
                    "provide a strong password instead (see .env.example)."
                )
                if production:
                    raise ValueError(insecure_message)
                warnings.warn(
                    f"{insecure_message} Permitted locally; production "
                    "rejects it.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            elif production and len(self.admin_password) < _MIN_ADMIN_PASSWORD_LENGTH:
                raise ValueError(
                    f"ADMIN_PASSWORD is too short for production use: "
                    f"provide at least "
                    f"{_MIN_ADMIN_PASSWORD_LENGTH} characters."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
