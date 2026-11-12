"""WP1 regression tests: database/init.sql security hygiene.

Background (Task 4 of Work Package 1): ``database/init.sql`` previously
created the application user with a hardcoded weak placeholder password
(``skipline_pass``).  That block was removed; user provisioning is now
environment-driven via the official MySQL image entrypoint
(``MYSQL_USER`` / ``MYSQL_PASSWORD``), and ``docker-compose.yml`` guards
the credentials with ``${VAR:?required}`` fail-fast substitution.

These tests lock in that fix at the source level (static assertions, no
database or container required):

1. No hardcoded credentials or account-management SQL remain in init.sql.
2. Database creation itself is intact (utf8mb4 / utf8mb4_unicode_ci).
3. Compose still fail-fasts when MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD are
   missing, and still mounts the (now inert) init.sql read-only.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_SQL = REPO_ROOT / "database" / "init.sql"
COMPOSE = REPO_ROOT / "docker-compose.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _executable_sql(path: Path) -> str:
    """Return only the executable SQL statements (comments stripped,
    original case).  The file documents the removed block inside SQL
    comments; those mention 'CREATE USER' etc. legitimately."""
    lines = []
    for raw in _read(path).splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("--"):
            continue
        lines.append(stripped)
    return "\n".join(lines)


class TestInitSqlHasNoHardcodedCredentials:
    """The removed weak-password block must never come back."""

    def test_no_create_user_statement(self) -> None:
        sql = _executable_sql(INIT_SQL)
        assert "CREATE USER" not in sql, (
            "database/init.sql must not provision users; account creation "
            "is environment-driven via the MySQL entrypoint (MYSQL_USER/"
            "MYSQL_PASSWORD)."
        )

    def test_no_identified_by_literal(self) -> None:
        sql = _executable_sql(INIT_SQL).upper()
        assert "IDENTIFIED BY" not in sql, (
            "database/init.sql must not embed any password literal."
        )

    def test_no_skipline_pass_password(self) -> None:
        sql = _executable_sql(INIT_SQL)
        assert "skipline_pass" not in sql, (
            "The removed hardcoded password must not be reintroduced."
        )

    def test_no_grant_or_flush_privileges(self) -> None:
        sql = _executable_sql(INIT_SQL).upper()
        assert "GRANT" not in sql and "FLUSH PRIVILEGES" not in sql, (
            "Privilege management no longer belongs in init.sql."
        )


class TestInitSqlDatabaseCreationIntact:
    """Removing the user block must not have damaged the DB creation."""

    def test_creates_skipline_database(self) -> None:
        sql = _executable_sql(INIT_SQL).upper()
        assert "CREATE DATABASE IF NOT EXISTS SKIPLINE" in sql

    def test_utf8mb4_charset(self) -> None:
        sql = _executable_sql(INIT_SQL).upper()
        assert "CHARACTER SET UTF8MB4" in sql
        assert "COLLATE UTF8MB4_UNICODE_CI" in sql


class TestComposeCredentialGuards:
    """Fail-fast environment guards keep initialization safe."""

    def test_root_password_is_required(self) -> None:
        compose = _read(COMPOSE)
        assert "${MYSQL_ROOT_PASSWORD:?" in compose, (
            "Compose must fail fast when MYSQL_ROOT_PASSWORD is missing."
        )

    def test_app_password_is_required(self) -> None:
        compose = _read(COMPOSE)
        assert "${DB_PASSWORD:?" in compose, (
            "Compose must fail fast when DB_PASSWORD is missing."
        )

    def test_entrypoint_user_provisioning_wired(self) -> None:
        compose = _read(COMPOSE)
        assert "MYSQL_USER" in compose and "MYSQL_PASSWORD" in compose, (
            "MYSQL_USER/MYSQL_PASSWORD hand user provisioning to the MySQL "
            "entrypoint (the official, env-driven mechanism)."
        )

    def test_init_sql_mounted_read_only(self) -> None:
        compose = _read(COMPOSE)
        assert "./database/init.sql:/docker-entrypoint-initdb.d/init.sql:ro" in compose, (
            "init.sql stays mounted read-only as the schema file."
        )
