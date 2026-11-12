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
# placeholder secrets. The two long change-me placeholders are included
# explicitly (prodcheck finding, Nov 2026): they were observed verbatim in a
# local development .env and previously PASSED the >=32 length check in
# production. Unfilled `<…>` marker values from .env.example itself are
# rejected by _is_unfilled_marker() below (second prodcheck finding, Nov 6).
_INSECURE_SECRET_VALUES = frozenset(
    {
        "changeme",
        "changeme-jwt",
        "secret",
        "password",
        "change-me",
        "change-me-to-a-64-char-random-string",
        "change-me-to-another-64-char-random-string",
    }
)
_INSECURE_ADMIN_PASSWORDS = frozenset(
    {"admin", "password", "changeme", "admin@1234!"}
)
# Strong required lengths for production.
_MIN_GLOBAL_SECRET_LENGTH = 32
_MIN_ADMIN_PASSWORD_LENGTH = 12


def _is_unfilled_marker(value: str) -> bool:
    """True when the ENTIRE value is one unfilled `.env.example` placeholder
    of the `<set-a-…>` template form (starts with `<`, ends with `>`).

    Narrow by design (prodcheck review, Nov 6): stray `<`/`>` characters in
    the middle of a legitimate value (a real password or secret can contain
    them) are NOT rejected, and malformed/truncated fragments are handled
    safely:

    - an incomplete marker fragment (e.g. `<set-a-long`, `foo<bar`) is not
      treated as a marker — but such a value still fails the production
      minimum-length requirement, and known placeholder strings remain
      covered exactly by `_INSECURE_SECRET_VALUES`;
    - `<` alone, `>` alone and whitespace-padded markers are rejected only in
      their complete `<…>` template form.
    - Deliberately NOT applied to DB_PASSWORD: a legitimate database password
      may legitimately contain `<`/`>`, and a wrong DB password fails loudly
      at first connection anyway.
    """
    if not value:
        return False
    v = value.strip()
    return len(v) >= 2 and v.startswith("<") and v.endswith(">")


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
    # WP2 (R2): least-privilege application identity (skipline_app) — root is
    # never a valid runtime value. Overridden by DB_USER in the environment.
    db_user: str = "skipline_app"
    db_password: str = ""

    # ── WebSocket scaling (Phase 6) ──────────────────────────────────────────
    # Empty = single-worker in-process delivery (default; tests/dev).
    # Set to e.g. redis://localhost:6379/0 for multi-worker pub/sub fan-out.
    redis_url: str = ""

    # ── Distributed rate limiting (Phase 8 — SEC-004) ────────────────────────
    # When redis_url is set, the rate limiter stores counters in Redis so
    # limits hold across all app workers; on a Redis outage it falls back
    # to the per-worker in-memory window (a warning is logged — protection
    # is degraded, never silently disabled).
    rate_limit_auth_requests: int = 20      # credential paths (login/register/password)
    rate_limit_account_requests: int = 300  # per authenticated principal

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

    # ── AI booking assistant (Phase 9 — Workstreams C & D, MASTER §7/§14/§15) ─
    # OpenAI-compatible HTTP chat endpoint. ALL fields default to EMPTY:
    # an unconfigured provider means the AI assistant is "unavailable" and
    # every endpoint degrades gracefully (never a 500, never a fabricated
    # booking) — the manual flow remains fully usable.
    # ai_provider:  "" (disabled) | "openai" | any OpenAI-compatible service
    ai_provider: str = ""
    ai_base_url: str = ""          # e.g. https://api.openai.com/v1
    ai_api_key: str = ""           # NEVER logged; sent only to ai_base_url
    ai_model: str = ""             # e.g. gpt-4o-mini (deployment-dependent)
    ai_request_timeout_seconds: float = 20.0
    ai_max_history_turns: int = 12  # bounded context (C11)
    ai_max_tool_rounds: int = 8     # bounded tool loop per user message
    # WP1 (2026-11-09): the minimum deterministic assistant turn chain is
    # 6 tool actions + 1 acknowledgment = 7 LLM rounds (search orgs →
    # list services → list counters → check availability → quote price →
    # prepare draft → reply). With the former cap of 6 the loop could
    # never reach the acknowledgment, so a valid draft existed but the UI
    # never received has_pending_draft and the Confirm bar never showed.
    # 8 leaves one spare round for a protocol repair.

    # ── Malware scanning (Phase 8 — SEC-003, local ClamAV) ───────────────────
    # Approved approach: LOCAL CLAMAV via the clamd INSTREAM protocol.
    # In production, run the clamav container/service and set
    # CLAMAV_ENABLED=true (the compose file ships a clamav service).
    # With scanning disabled, uploads are accepted but explicitly recorded
    # with scan_status="disabled" (documented gap, never "clean").
    clamav_enabled: bool = False
    clamav_host: str = "127.0.0.1"
    clamav_port: int = 3310
    clamav_scan_timeout_seconds: float = 10.0

    # ── CSRF (Phase 8 — SEC-004) ─────────────────────────────────────────────
    # Enforcement is automatic in production; set CSRF_ENFORCE=true to
    # exercise the middleware in development / integration tests.
    csrf_enforce: bool = False

    # ── File logging (Phase 9 deployment hardening — app_logs volume) ───────
    # Optional: when LOG_DIR is set (compose sets /app/logs), the process
    # additionally appends to a rotated file in that directory in addition
    # to the default stderr sink; empty (default) keeps stderr-only. No PII:
    # request logs contain only method/path/status/duration and audit
    # entries; secrets are never logged anywhere.
    log_dir: str = ""
    log_file_level: str = "INFO"

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
        elif value.strip().lower() in _INSECURE_SECRET_VALUES \
                or _is_unfilled_marker(value):
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
        if production and self.app_debug:
            # APP_DEBUG drives FastAPI debug pages (traceback disclosure)
            # and SQLAlchemy SQL echo (raw query leakage). Debug mode in a
            # production deployment is a hard configuration error, matching
            # the "no insecure default is ever substituted" doctrine.
            raise ValueError(
                "APP_DEBUG=true is not permitted when APP_ENV=production. "
                "Set APP_DEBUG=false (or rely on the compose default)."
            )
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
            if lowered in _INSECURE_ADMIN_PASSWORDS \
                    or _is_unfilled_marker(self.admin_password):
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
