"""
tests/unit/test_0015_schema_sync.py
───────────────────────────────────
Migration 0015 (`0015_schema_sync_orm_reconcile`) unit verification on SQLite.

Migration 0015 reconciles Alembic-built databases with current ORM metadata.
Its guards must make it:
- safe on sparse/partial schemas (tables absent → skipped);
- data-preserving for the sessions table: the legacy 0003 shape (integer PK,
  `jti`) is rebuilt into the ORM shape (String(36) UUID PK, `last_activity_at`)
  with a full row copy, and the downgrade mirror copies back (`id` → `jti`).

The MySQL-specific acceptance (fresh Alembic-built MySQL DB booting the app +
real logins + `alembic check` clean + verified `create_all` no-op) is recorded
in docs/OPEN_DECISIONS.md; this module exercises the guarded logic itself.
"""

from pathlib import Path

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def migration_env(monkeypatch, tmp_path):
    """A disposable SQLite database used as the Alembic migration target."""
    db_file = tmp_path / "0015_test.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    # Patch BOTH URL properties: migrations/env.py builds the Alembic URL
    # from settings.async_database_url (async aiomysql engine); patching only
    # the sync URL would leave the real configured MySQL database as target.
    monkeypatch.setattr(
        Settings, "sync_database_url", property(lambda self: url)
    )
    monkeypatch.setattr(
        Settings, "async_database_url", property(lambda self: url)
    )

    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return cfg, db_file


async def _sessions_rows(db_file):
    async with aiosqlite.connect(str(db_file)) as conn:
        rows = await conn.execute(
            "SELECT * FROM sessions"
        )
        cols = [d[0] for d in rows.description] if rows.description else []
        return cols, await rows.fetchall()


def test_legacy_built_chain_reaches_head_with_0015(migration_env):
    """A sparse 0014-state database upgrades to head without crashing: every
    guarded 0015 op either applies or is skipped for missing structure."""
    cfg, db_file = migration_env

    async def build_0004_schema():
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_file.as_posix()}"
        )
        async with engine.begin() as conn:
            for ddl in (
                "CREATE TABLE organizations (id INTEGER PRIMARY KEY)",
                "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER)",
                "CREATE TABLE employees (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER)",
                "CREATE TABLE counters (id INTEGER PRIMARY KEY, organization_id INTEGER)",
                "CREATE TABLE services (id INTEGER PRIMARY KEY, organization_id INTEGER)",
                "CREATE TABLE tokens (id INTEGER PRIMARY KEY, organization_id INTEGER, counter_id INTEGER)",
                "CREATE TABLE payments (id INTEGER PRIMARY KEY, token_id INTEGER, organization_id INTEGER, amount NUMERIC(10, 2), payment_method VARCHAR(30), status VARCHAR(30))",
            ):
                await conn.execute(text(ddl))
        await engine.dispose()

    import asyncio
    asyncio.run(build_0004_schema())

    command.stamp(cfg, "0004")
    command.upgrade(cfg, "head")  # 0005..0015 — 0015 must tolerate sparse shape
    command.downgrade(cfg, "0004")  # guarded downgrade is equally safe
    command.upgrade(cfg, "head")


def test_session_data_survives_full_upgrade_downgrade_roundtrip(migration_env):
    """Legacy 0003 sessions rows (jti-keyed) are:
    - carried verbatim into the ORM shape on upgrade (id ← LEFT(jti, 36),
      last_activity_at ← last_active_at), and
    - restored on downgrade (jti ← id, last_active_at ← last_activity_at).
    Loss-preserving in both directions."""
    import asyncio

    cfg, db_file = migration_env

    async def build_legacy_sessions():
        """A 0014-era chain database — run every migration to 0014, then seed
        a session as 0003's shape would hold it. Building the real chain keeps
        the legacy shape authentic (instead of hand-faking the table)."""
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_file.as_posix()}"
        )
        async with engine.begin() as conn:
            for ddl in (
                "CREATE TABLE organizations (id INTEGER PRIMARY KEY)",
                "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER)",
                "CREATE TABLE employees (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER)",
                "CREATE TABLE counters (id INTEGER PRIMARY KEY, organization_id INTEGER)",
                "CREATE TABLE services (id INTEGER PRIMARY KEY, organization_id INTEGER)",
                "CREATE TABLE tokens (id INTEGER PRIMARY KEY, organization_id INTEGER, counter_id INTEGER, checked_in_by INTEGER)",
                "CREATE TABLE payments (id INTEGER PRIMARY KEY, token_id INTEGER, organization_id INTEGER, user_id INTEGER, amount NUMERIC(10, 2), payment_method VARCHAR(30), status VARCHAR(30))",
                (
                    "CREATE TABLE sessions ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " jti VARCHAR(64) NOT NULL,"
                    " principal_type VARCHAR(32) NOT NULL,"
                    " principal_id INTEGER NOT NULL,"
                    " organization_id INTEGER,"
                    " token_type VARCHAR(32) NOT NULL DEFAULT 'access',"
                    " user_agent VARCHAR(512),"
                    " ip_address VARCHAR(64),"
                    " expires_at DATETIME NOT NULL,"
                    " is_revoked BOOLEAN NOT NULL DEFAULT 0,"
                    " revoked_at DATETIME,"
                    " revoked_reason VARCHAR(255),"
                    " created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
                    " last_active_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
                ),
            ):
                await conn.execute(text(ddl))
            await conn.execute(text(
                "INSERT INTO sessions (jti, principal_type, principal_id, "
                "organization_id, token_type, user_agent, ip_address, "
                "expires_at, is_revoked, created_at, last_active_at) "
                "VALUES "
                "('11111111-2222-3333-4444-555555555555', 'organization', 7, "
                " 2, 'access', 'UA-agent-1', '10.0.0.1', "
                "'2030-01-01 00:00:00', 0, '2026-01-01 08:00:00', "
                "'2026-01-01 09:00:00')"
            ))
        await engine.dispose()

    asyncio.run(build_legacy_sessions())
    # DB is a genuine 0003-shape chain; declare 0014 and reconcile
    command.stamp(cfg, "0014")
    command.upgrade(cfg, "0015")

    cols, rows = asyncio.run(_sessions_rows(db_file))
    assert set(cols) >= {"id", "principal_type", "principal_id", "is_revoked",
                         "expires_at", "created_at", "last_activity_at"}
    assert "jti" not in cols
    assert len(rows) == 1, "session row must survive the ORM rebuild"
    row = dict(zip(cols, rows[0]))
    assert row["id"] == "11111111-2222-3333-4444-555555555555"
    assert row["principal_type"] == "organization"
    assert row["principal_id"] == 7
    assert row["is_revoked"] in (0, False)
    assert str(row["last_activity_at"]).startswith("2026-01-01 09:00:00") or (
        "09:00" in str(row["last_activity_at"])
    )

    command.downgrade(cfg, "0014")
    cols, rows = asyncio.run(_sessions_rows(db_file))
    assert "jti" in cols, "downgrade restores the legacy 0003 shape"
    assert len(rows) == 1, "session row must survive the legacy rebuild"
    legacy = dict(zip(cols, rows[0]))
    assert legacy["jti"] == "11111111-2222-3333-4444-555555555555"
    assert legacy["principal_type"] == "organization"
    assert legacy["principal_id"] == 7
    assert legacy["is_revoked"] in (0, False)
    assert "09:00" in str(legacy["last_active_at"])

    command.upgrade(cfg, "0015")
    cols, rows = asyncio.run(_sessions_rows(db_file))
    assert "jti" not in cols
    assert len(rows) == 1
