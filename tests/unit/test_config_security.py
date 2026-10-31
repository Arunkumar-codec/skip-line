"""
tests/unit/test_config_security.py
──────────────────────────────────
Focused regression tests for database-credential configuration security:

- no hardcoded/insecure default database password ships in the code
- the password is loaded from the DB_PASSWORD environment variable
- a missing password is a hard error in production and a visible warning
  elsewhere (no silent insecure substitution)
- .env.example contains only placeholders, never real credentials

Secret values used here are synthetic dummies; assertions never echo them.
"""

import re
import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy import func as sa_func

from app.config import Settings, settings
from app.services.admin_service import seed_default_admin
from app.utils.security import verify_password
from models.admin import Admin

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

# Synthetic dummy credentials for tests — deliberately NOT real values.
DUMMY_PASSWORD = "dummy-test-password-for-config-tests"
DUMMY_ADMIN_PASSWORD = "dummy-admin-bootstrap-password!"
DUMMY_STRONG_SECRET = "dummy-signing-secret-for-config-tests-0123456789abcdef"
DUMMY_STRONG_JWT_SECRET = "dummy-jwt-secret-for-config-tests-0123456789abcdef"


def build_settings(**kwargs) -> Settings:
    """Build a Settings instance isolated from the developer's .env file and
    any process environment, so tests are deterministic."""
    kwargs["_env_file"] = None  # never read the developer's .env
    return Settings(**kwargs)


def clear_secret_env(monkeypatch):
    """Remove every secret-related variable (all case variants) so tests see
    deterministic, isolated configuration."""
    for name in [
        "DB_PASSWORD", "db_password",
        "SECRET_KEY", "secret_key",
        "JWT_SECRET_KEY", "jwt_secret_key",
        "ADMIN_PASSWORD", "admin_password",
    ]:
        monkeypatch.delenv(name, raising=False)


class TestDbPasswordConfiguration:
    def test_no_hardcoded_default_password(self, monkeypatch):
        """The insecure hardcoded default must be gone: absent DB_PASSWORD
        resolves to an empty password, never a hidden credential."""
        clear_secret_env(monkeypatch)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # dev warning expected
            cfg = build_settings()
        assert cfg.db_password == ""

    def test_password_loaded_from_environment(self, monkeypatch):
        """DB_PASSWORD must be picked up from the environment by the existing
        settings mechanism (pydantic-settings, case-insensitive name)."""
        clear_secret_env(monkeypatch)
        monkeypatch.setenv("DB_PASSWORD", DUMMY_PASSWORD)
        cfg = build_settings()
        assert cfg.db_password == DUMMY_PASSWORD

    def test_password_survives_url_encoding(self, monkeypatch):
        """Special characters in DB_PASSWORD must be URL-encoded in both the
        sync (Alembic) and async database URLs."""
        clear_secret_env(monkeypatch)
        special = "p@ss:word/with#chars"
        monkeypatch.setenv("DB_PASSWORD", special)
        cfg = build_settings()
        assert "p%40ss%3Aword%2Fwith%23chars" in cfg.async_database_url
        assert "p%40ss%3Aword%2Fwith%23chars" in cfg.sync_database_url
        # the raw password must not appear unencoded in either URL
        assert special not in cfg.async_database_url
        assert special not in cfg.sync_database_url


class TestMissingProductionCredentials:
    def test_missing_password_fails_hard_in_production(self, monkeypatch):
        """app_env=production without DB_PASSWORD must raise a clear
        configuration error naming the required variable — never start with
        an insecure fallback."""
        clear_secret_env(monkeypatch)
        with pytest.raises(ValidationError) as excinfo:
            build_settings(app_env="production")
        assert "DB_PASSWORD" in str(excinfo.value)

    def test_missing_password_warns_but_allows_development(self, monkeypatch):
        """Local development (and SQLite-backed tests) stay compatible, with a
        visible warning instead of a silent insecure substitution."""
        clear_secret_env(monkeypatch)
        with pytest.warns(RuntimeWarning, match="DB_PASSWORD"):
            cfg = build_settings()
        assert cfg.db_password == ""

    def test_configured_password_suppresses_development_warning(
        self, monkeypatch
    ):
        clear_secret_env(monkeypatch)
        monkeypatch.setenv("DB_PASSWORD", DUMMY_PASSWORD)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = build_settings()
        assert cfg.db_password == DUMMY_PASSWORD
        # no DB_PASSWORD warning specifically; unrelated secret warnings
        # (generated ephemeral signing keys) are expected in development
        assert not any(
            "DB_PASSWORD" in str(w.message) for w in caught
        )


class TestEnvExamplePlaceholders:
    def test_env_example_documents_required_db_password(self):
        content = ENV_EXAMPLE.read_text(encoding="utf-8")
        assert "DB_PASSWORD" in content

    def test_env_example_contains_only_placeholders(self):
        """Every DB_PASSWORD value in .env.example must be an obvious
        placeholder — never a real credential."""
        db_lines = [
            line
            for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line.lstrip().startswith("DB_PASSWORD=")
        ]
        assert len(db_lines) == 1
        value = db_lines[0].split("=", 1)[1].strip()
        # an obvious placeholder, never a real credential
        assert "<" in value or value == ""


class TestGlobalSigningSecrets:
    @pytest.mark.parametrize("env_name", ["SECRET_KEY", "JWT_SECRET_KEY"])
    def test_missing_signing_secret_fails_hard_in_production(
        self, monkeypatch, env_name
    ):
        monkeypatch.setenv("DB_PASSWORD", DUMMY_PASSWORD)
        monkeypatch.setenv("ADMIN_PASSWORD", DUMMY_ADMIN_PASSWORD)
        monkeypatch.delenv(env_name, raising=False)
        monkeypatch.delenv(env_name.lower(), raising=False)
        with pytest.raises(ValidationError) as excinfo:
            build_settings(app_env="production")
        assert env_name in str(excinfo.value)
        # no secret value ever leaks into error/warning text
        assert DUMMY_PASSWORD not in str(excinfo.value)
        assert DUMMY_ADMIN_PASSWORD not in str(excinfo.value)

    @pytest.mark.parametrize("env_name,insecure", [
        ("SECRET_KEY", "changeme"),
        ("JWT_SECRET_KEY", "changeme-jwt"),
    ])
    def test_insecure_signing_secret_rejected_in_production(
        self, monkeypatch, env_name, insecure
    ):
        monkeypatch.setenv("DB_PASSWORD", DUMMY_PASSWORD)
        monkeypatch.setenv("ADMIN_PASSWORD", DUMMY_ADMIN_PASSWORD)
        monkeypatch.setenv(env_name, insecure)
        with pytest.raises(ValidationError) as excinfo:
            build_settings(app_env="production")
        assert env_name in str(excinfo.value)

    def test_too_short_signing_secret_rejected_in_production(self, monkeypatch):
        monkeypatch.setenv("DB_PASSWORD", DUMMY_PASSWORD)
        monkeypatch.setenv("ADMIN_PASSWORD", DUMMY_ADMIN_PASSWORD)
        monkeypatch.setenv("SECRET_KEY", "insecure-short-secret")
        with pytest.raises(ValidationError) as excinfo:
            build_settings(app_env="production")
        assert "SECRET_KEY" in str(excinfo.value)

    @pytest.mark.parametrize("name,env_name", [
        ("secret_key", "SECRET_KEY"),
        ("jwt_secret_key", "JWT_SECRET_KEY"),
    ])
    def test_missing_signing_secret_generated_in_development(
        self, monkeypatch, name, env_name
    ):
        clear_secret_env(monkeypatch)
        with pytest.warns(RuntimeWarning, match=env_name):
            cfg = build_settings()
        generated = getattr(cfg, name)
        # securely generated, strong, and not a known insecure default
        assert len(generated) >= 32
        assert generated.strip().lower() not in {"changeme", "changeme-jwt"}
        assert generated != "changeme" and generated != "changeme-jwt"

    def test_insecure_signing_secret_warned_in_development(self, monkeypatch):
        clear_secret_env(monkeypatch)
        monkeypatch.setenv("SECRET_KEY", "changeme")
        with pytest.warns(RuntimeWarning, match="SECRET_KEY"):
            cfg = build_settings()
        # a locally chosen value is honored, but never silently accepted
        assert cfg.secret_key == "changeme"


class TestAdminPasswordConfiguration:
    def test_no_hardcoded_admin_password(self, monkeypatch):
        clear_secret_env(monkeypatch)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            cfg = build_settings()
        assert cfg.admin_password == ""

    def test_admin_password_loaded_from_environment(self, monkeypatch):
        clear_secret_env(monkeypatch)
        monkeypatch.setenv("ADMIN_PASSWORD", DUMMY_ADMIN_PASSWORD)
        cfg = build_settings()
        assert cfg.admin_password == DUMMY_ADMIN_PASSWORD

    @pytest.mark.parametrize("insecure", ["Admin@1234!", "admin", "password"])
    def test_insecure_admin_password_rejected_in_production(
        self, monkeypatch, insecure
    ):
        monkeypatch.setenv("DB_PASSWORD", DUMMY_PASSWORD)
        monkeypatch.setenv("SECRET_KEY", DUMMY_STRONG_SECRET)
        monkeypatch.setenv("JWT_SECRET_KEY", DUMMY_STRONG_JWT_SECRET)
        monkeypatch.setenv("ADMIN_PASSWORD", insecure)
        with pytest.raises(ValidationError) as excinfo:
            build_settings(app_env="production")
        assert "ADMIN_PASSWORD" in str(excinfo.value)

    def test_short_admin_password_rejected_in_production(self, monkeypatch):
        monkeypatch.setenv("DB_PASSWORD", DUMMY_PASSWORD)
        monkeypatch.setenv("SECRET_KEY", DUMMY_STRONG_SECRET)
        monkeypatch.setenv("JWT_SECRET_KEY", DUMMY_STRONG_JWT_SECRET)
        monkeypatch.setenv("ADMIN_PASSWORD", "short")
        with pytest.raises(ValidationError) as excinfo:
            build_settings(app_env="production")
        assert "ADMIN_PASSWORD" in str(excinfo.value)


class TestDefaultAdminSeeding:
    """Seeding tests use a dedicated fresh in-memory database: the shared
    conftest database may already contain committed admins from other tests
    (seed_default_admin commits, so session rollback cannot undo them)."""

    async def _make_fresh_session(self):
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

        from app.database import Base
        import models  # noqa: F401  (register tables)

        self._engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return AsyncSession(bind=self._engine, expire_on_commit=False)

    async def _dispose(self):
        await self._engine.dispose()

    async def _admin_count(self, session) -> int:
        result = await session.execute(
            select(sa_func.count()).select_from(Admin)
        )
        return result.scalar()

    async def test_seed_skips_without_password(self, monkeypatch):
        """An unset ADMIN_PASSWORD must not seed an admin with an empty or
        fake password — provisioning is skipped with a warning instead."""
        from loguru import logger

        monkeypatch.setattr(
            "app.services.admin_service.settings.admin_password", ""
        )
        log_messages: list = []
        handler_id = logger.add(log_messages.append, level="WARNING")
        session = await self._make_fresh_session()
        try:
            await seed_default_admin(session)
            count = await self._admin_count(session)
        finally:
            logger.remove(handler_id)
            await self._dispose()
        assert count == 0
        assert any("ADMIN_PASSWORD" in message for message in log_messages)

    async def test_seed_creates_admin_with_configured_password(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "app.services.admin_service.settings.admin_password",
            DUMMY_ADMIN_PASSWORD,
        )
        session = await self._make_fresh_session()
        try:
            await seed_default_admin(session)
            result = await session.execute(
                select(Admin).where(Admin.email == settings.admin_email)
            )
            admin = result.scalar_one_or_none()
            assert admin is not None
            assert verify_password(DUMMY_ADMIN_PASSWORD, admin.password_hash)
        finally:
            await self._dispose()

    async def test_seed_idempotent_when_admin_exists(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.admin_service.settings.admin_password",
            DUMMY_ADMIN_PASSWORD,
        )
        session = await self._make_fresh_session()
        try:
            await seed_default_admin(session)
            await seed_default_admin(session)
            assert await self._admin_count(session) == 1
        finally:
            await self._dispose()


class TestEnvExampleSecurityPlaceholders:
    def test_env_example_documents_all_required_secrets(self):
        content = ENV_EXAMPLE.read_text(encoding="utf-8")
        for variable in (
            "DB_PASSWORD", "SECRET_KEY", "JWT_SECRET_KEY", "ADMIN_PASSWORD"
        ):
            assert variable in content, f"{variable} must be documented"


class TestHardcodedFallbackCredentials:
    """docker-compose.yml and the Makefile must not silently substitute
    insecure built-in passwords when required credential variables are
    missing — they fail with a clear error instead."""

    COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
    MAKEFILE = PROJECT_ROOT / "Makefile"

    _INSECURE_FALLBACK_LITERALS = (
        "skipline_pass",
        "rootpassword",
        "changeme",
        "changeme-jwt",
        "Admin@1234!",
    )
    # The former hardcoded DB password is detected by format pattern so the
    # live credential value is not duplicated into test files (it must only
    # exist in .env and on the database server).
    _INSECURE_FALLBACK_PATTERNS = (
        re.compile(r"Arun\d{6,}"),
    )

    def test_compose_has_no_insecure_fallback_literals(self):
        content = self.COMPOSE_FILE.read_text(encoding="utf-8")
        for literal in self._INSECURE_FALLBACK_LITERALS:
            assert literal not in content, (
                f"docker-compose.yml still contains insecure fallback "
                f"credential literal '{literal}'"
            )
        for pattern in self._INSECURE_FALLBACK_PATTERNS:
            assert not pattern.search(content), (
                "docker-compose.yml still contains a hardcoded database "
                "password matching the former default format"
            )

    def test_compose_db_password_is_required(self):
        content = self.COMPOSE_FILE.read_text(encoding="utf-8")
        assert "${DB_PASSWORD:?" in content, (
            "MYSQL_PASSWORD must be backed by a required DB_PASSWORD "
            "variable (${VAR:?...}), not a silent fallback"
        )
        assert "?DB_PASSWORD:-" not in content  # no default-value fallback

    def test_compose_mysql_root_password_is_required(self):
        content = self.COMPOSE_FILE.read_text(encoding="utf-8")
        # required in both the environment block and the healthcheck
        assert content.count("${MYSQL_ROOT_PASSWORD:?") >= 2, (
            "MYSQL_ROOT_PASSWORD must be required everywhere it is used "
            "(environment + healthcheck), with no silent fallback"
        )
        assert "?MYSQL_ROOT_PASSWORD:-" not in content

    def test_compose_is_valid_yaml(self):
        try:
            import yaml
        except ImportError:  # pragma: no cover
            pytest.skip("PyYAML not installed")
        import io

        text = self.COMPOSE_FILE.read_text(encoding="utf-8")
        # blank out interpolations so plain YAML parsing succeeds
        sanitized = re.sub(r"\$\{[^}]*\}", "PLACEHOLDER", text)
        document = yaml.safe_load(io.StringIO(sanitized))
        assert document["services"]["db"]["environment"]["MYSQL_PASSWORD"] == "PLACEHOLDER"
        assert document["services"]["db"]["environment"]["MYSQL_ROOT_PASSWORD"] == "PLACEHOLDER"

    def test_makefile_has_no_insecure_fallback_literals(self):
        content = self.MAKEFILE.read_text(encoding="utf-8")
        for literal in ("skipline_pass", "Admin@1234!"):
            assert literal not in content, (
                f"Makefile still contains insecure fallback credential "
                f"literal '{literal}'"
            )
        for pattern in self._INSECURE_FALLBACK_PATTERNS:
            assert not pattern.search(content), (
                "Makefile still contains a hardcoded database password "
                "matching the former default format"
            )

    def test_makefile_seed_requires_db_password(self):
        content = self.MAKEFILE.read_text(encoding="utf-8")
        assert "${DB_PASSWORD:?" in content, (
            "make seed must fail with a clear error when DB_PASSWORD is "
            "missing (shell ':?' expansion), not fall back to a password"
        )
        assert ":?DB_PASSWORD:-" not in content  # no default fallback

    def test_makefile_loads_dotenv_when_present(self):
        content = self.MAKEFILE.read_text(encoding="utf-8")
        assert "include .env" in content, (
            "Makefile must load .env so development keeps working without "
            "exporting variables manually"
        )
        # guarded include so a missing .env is not an error
        guard_index = content.find("ifneq ($(wildcard .env),)")
        assert guard_index != -1, "the .env include must be guarded by a wildcard check"

    def test_env_example_documents_mysql_root_password(self):
        content = ENV_EXAMPLE.read_text(encoding="utf-8")
        assert "MYSQL_ROOT_PASSWORD" in content
