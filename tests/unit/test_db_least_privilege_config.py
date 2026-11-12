"""WP2 regression tests: database least-privilege configuration (R2–R4).

Background (Work Package 2): the application runtime previously connected to
MySQL as ``root``. WP2 transitioned the runtime identity to a dedicated
least-privilege user (``skipline_app``), rotated the host MySQL root
password, and hardened docker-compose.yml so no weak default identity or
hardcoded password can slip into a deployment.

These tests lock the configuration in at the source level (static
assertions over docker-compose.yml / .env.example / app/config.py — no
database, container, or network required):

1. docker-compose.yml contains no hardcoded passwords and never passes
   ``root`` as the application database user.
2. Credential variables use fail-fast (``${VAR:?required}``) guards.
3. The config default user is the least-privilege identity (never root).
4. ``.env.example`` documents the least-privilege identity with placeholder
   (never real-looking) passwords.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

CREDS_VAR = re.compile(r"\$\{([A-Z_]+)(:[\?=][^}]*)?\}")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _password_lines(compose: str) -> list[tuple[str, str]]:
    """Return (yaml_key, value) pairs for every PASSWORD-bearing env line."""
    out = []
    for line in compose.splitlines():
        stripped = line.strip()
        m = re.match(r"([A-Z_]*PASSWORD[A-Z_]*):\s*(.+)$", stripped)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


class TestComposeHasNoHardcodedPasswords:
    """docker-compose.yml must never carry a literal password value."""

    def test_password_values_are_env_references_only(self) -> None:
        compose = _read(COMPOSE)
        pairs = _password_lines(compose)
        assert pairs, "compose must declare its credential variables"
        for key, value in pairs:
            assert value.startswith("${"), (
                f"{key} must be an environment-variable reference, "
                f"never a literal value (got: {value})")

    def test_no_weak_placeholder_passwords(self) -> None:
        compose = _read(COMPOSE)
        assert "skipline_pass" not in compose, (
            "the removed init.sql placeholder must never reappear in compose")
        for key, value in _password_lines(compose):
            assert "skipline_pass" not in value
            assert not re.search(r"[\"'][^${]*[A-Za-z0-9!@#$%^&*]{6,}[\"']", value), (
                f"{key} looks like a quoted literal password: {value}")

    def test_healthcheck_root_use_is_container_internal_only(self) -> None:
        # The db healthcheck authenticates to its OWN container's MySQL as
        # root (mysqladmin ping) — that is container-internal administration,
        # not the application runtime identity. Assert it stays that way and
        # is the ONLY root usage in the file's actual YAML (comments may
        # document the policy and mention the word).
        compose = _read(COMPOSE)
        code_lines = [l for l in compose.splitlines() if not l.strip().startswith("#")]
        root_uses = [l.strip() for l in code_lines if re.search(r"\broot\b", l)]
        healthcheck_uses = [l for l in root_uses if "mysqladmin" in l or "healthcheck" in l.lower()]
        non_healthcheck = [l for l in root_uses if l not in healthcheck_uses]
        assert not non_healthcheck, (
            f"any root use outside the db container healthcheck is forbidden: "
            f"{non_healthcheck}")


class TestComposeNeverPassesRootAsAppUser:
    """The application/backup runtime identity must never be root."""

    def test_mysql_user_is_db_user_reference(self) -> None:
        compose = _read(COMPOSE)
        m = re.search(r"^\s*MYSQL_USER:\s*(\S+)", compose, re.MULTILINE)
        assert m, "db service must declare MYSQL_USER"
        value = m.group(1)
        assert value.startswith("${"), f"MYSQL_USER must come from DB_USER: {value}"
        assert "root" not in value, f"MYSQL_USER must never default to root: {value}"

    def test_no_db_user_literal_root(self) -> None:
        compose = _read(COMPOSE)
        for line in compose.splitlines():
            m = re.match(r"\s*(DB_USER|MYSQL_USER):\s*(\S+)", line)
            if m:
                assert "root" not in m.group(2).lower(), (
                    f"{m.group(1)} must never be root: {line.strip()}")


class TestComposeRequiredCredentialGuards:
    """All credential variables fail fast when unset (no silent defaults)."""

    def test_root_password_guard(self) -> None:
        compose = _read(COMPOSE)
        m = re.search(r"MYSQL_ROOT_PASSWORD:\s*\$\{MYSQL_ROOT_PASSWORD:(\?)", compose)
        assert m, "MYSQL_ROOT_PASSWORD must use a ${VAR:?required} fail-fast guard"

    def test_db_password_guard(self) -> None:
        compose = _read(COMPOSE)
        assert re.search(
            r"MYSQL_PASSWORD:\s*\$\{DB_PASSWORD:\?", compose), (
            "MYSQL_PASSWORD must use a ${VAR:?required} fail-fast guard")

    def test_db_user_guard(self) -> None:
        compose = _read(COMPOSE)
        assert re.search(r"MYSQL_USER:\s*\$\{DB_USER:\?", compose), (
            "MYSQL_USER must use a ${VAR:?required} fail-fast guard "
            "(WP2: no weak default identity)")

    def test_backup_service_db_user_guard(self) -> None:
        compose = _read(COMPOSE)
        backup_block = compose[compose.index("backup:"):]
        assert re.search(r"DB_USER:\s*\$\{DB_USER:\?", backup_block), (
            "the backup service must require DB_USER (never a fallback identity)")


class TestConfigDefaultIdentity:
    """The Settings default runtime user is the least-privilege identity."""

    def test_default_db_user_is_skipline_app(self) -> None:
        from app.config import Settings

        default = Settings.model_fields["db_user"].default
        assert default == "skipline_app", (
            f"runtime default user must be the least-privilege identity "
            f"skipline_app (got: {default})")

    def test_default_db_user_is_never_root(self) -> None:
        from app.config import Settings

        default = Settings.model_fields["db_user"].default
        assert default != "root"
        assert "root" not in str(default).lower()

    def test_db_password_has_no_default_value(self) -> None:
        from app.config import Settings

        field = Settings.model_fields["db_password"]
        assert field.default == "", (
            "db_password must have an empty default (production validator "
            "rejects it; no insecure value may ever be substituted)")


class TestEnvExampleDocumentsLeastPrivilege:
    """.env.example must teach the operator the least-privilege identity."""

    def test_db_user_is_skipline_app(self) -> None:
        example = _read(ENV_EXAMPLE)
        m = re.search(r"^DB_USER=(\S+)", example, re.MULTILINE)
        assert m, ".env.example must document DB_USER"
        assert m.group(1) == "skipline_app", (
            f".env.example must document the least-privilege user "
            f"skipline_app (got: {m.group(1)})")
        assert "root" not in m.group(1)

    def test_passwords_are_placeholders_only(self) -> None:
        example = _read(ENV_EXAMPLE)
        for key in ("DB_PASSWORD", "MYSQL_ROOT_PASSWORD"):
            m = re.search(rf"^{key}=(.*)$", example, re.MULTILINE)
            assert m, f".env.example must document {key}"
            value = m.group(1).strip()
            assert value.startswith("<set-a-strong-"), (
                f"{key} must be a placeholder, never a real-looking value "
                f"(got: {value[:20]}…)")
            assert len(value) < 60 and " " not in value, (
                f"{key} placeholder looks too real: {key}={value}")
