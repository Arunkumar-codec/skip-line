"""
tests/unit/test_migration_0016.py
─────────────────────────────────
Migration 0016 (`0016_trust_feedback_support_kyc`) unit verification on
a fully disposable SQLite database — the real configured database is
NEVER touched (both URL properties are patched, following
test_0015_schema_sync.py).

Strategy mirrors the baseline 0015 test: a sparse 0015-state schema is
hand-built (only the tables/columns migration 0016 touches), then
`stamp 0015 → upgrade 0016 → downgrade → re-upgrade` runs the real
 Alembic chain. (A full 0001..0015 chain upgrade on SQLite is blocked by
0015's `create_unique_constraint` — a pre-existing limitation documented
in docs/OPEN_DECISIONS.md; MySQL is the verified full-chain target.)

Covers:
- Upgrade creates all eight Phase 7 tables and the expanded columns.
- A branch row existing before the upgrade survives BOTH directions
  (branches/organization_verifications changes are additive, data loss-free).
- Downgrade removes the Phase 7 tables/columns without losing legacy rows.
"""

import asyncio
from pathlib import Path

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]

PHASE7_TABLES = {
    "feedback", "feedback_responses", "support_cases", "case_participants",
    "case_messages", "case_attachments", "knowledge_solutions",
    "organization_kyc",
}

# Minimal 0015-state schema: every table migration 0016 references via FK
# or ALTER, with the columns the seeded rows need.
SPARSE_0015_DDL = (
    "CREATE TABLE admins (id INTEGER PRIMARY KEY)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, status VARCHAR(30))",
    "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, "
    "name VARCHAR(200), branch_code VARCHAR(50), address TEXT, city VARCHAR(100), "
    "state VARCHAR(100))",
    "CREATE TABLE employees (id INTEGER PRIMARY KEY)",
    "CREATE TABLE counters (id INTEGER PRIMARY KEY)",
    "CREATE TABLE services (id INTEGER PRIMARY KEY, organization_id INTEGER)",
    "CREATE TABLE tokens (id INTEGER PRIMARY KEY, organization_id INTEGER, "
    "user_id INTEGER, counter_id INTEGER, service_id INTEGER, status VARCHAR(30))",
    "CREATE TABLE payments (id INTEGER PRIMARY KEY, organization_id INTEGER, "
    "user_id INTEGER)",
    "CREATE TABLE organization_verifications (id INTEGER PRIMARY KEY, "
    "organization_id INTEGER)",
)


@pytest.fixture
def migration_env(monkeypatch, tmp_path):
    """A disposable SQLite database used as the Alembic migration target."""
    db_file = tmp_path / "0016_test.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    # Patch BOTH URL properties (see test_0015_schema_sync.py comment):
    # migrations/env.py builds the Alembic URL from
    # settings.async_database_url; patching only the sync URL would leave
    # the real configured MySQL database as target.
    monkeypatch.setattr(
        Settings, "sync_database_url", property(lambda self: url)
    )
    monkeypatch.setattr(
        Settings, "async_database_url", property(lambda self: url)
    )
    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return cfg, db_file


async def _table_names(db_file):
    async with aiosqlite.connect(str(db_file)) as conn:
        rows = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        return {r[0] for r in await rows.fetchall()}


async def _table_cols(db_file, table):
    async with aiosqlite.connect(str(db_file)) as conn:
        rows = await conn.execute(f"PRAGMA table_info({table})")
        return {r[1] for r in await rows.fetchall()}


async def _branch_rows(db_file):
    async with aiosqlite.connect(str(db_file)) as conn:
        rows = await conn.execute("SELECT id, organization_id, name FROM branches")
        return await rows.fetchall()


async def _build_sparse_0015(db_file):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    async with engine.begin() as conn:
        for ddl in SPARSE_0015_DDL:
            await conn.execute(text(ddl))
        await conn.execute(text(
            "INSERT INTO organizations (id, status) VALUES (1, 'approved')"
        ))
        await conn.execute(text(
            "INSERT INTO branches (id, organization_id, name, branch_code, "
            "address, city, state) VALUES (1, 1, 'Main Branch', 'MAIN', "
            "'1 Kernel St', 'Metro', 'ST')"
        ))
    await engine.dispose()


def test_0016_roundtrip_preserves_existing_data(migration_env):
    cfg, db_file = migration_env

    asyncio.run(_build_sparse_0015(db_file))
    command.stamp(cfg, "0015")

    # ── upgrade → Phase 7 tables + expanded columns exist ──
    command.upgrade(cfg, "0016")
    tables = asyncio.run(_table_names(db_file))
    assert PHASE7_TABLES <= tables, "all Phase 7 tables must exist after upgrade"

    branch_cols = asyncio.run(_table_cols(db_file, "branches"))
    assert {"kyc_verified", "kyc_verified_at", "kyc_verified_by"} <= branch_cols
    ver_cols = asyncio.run(_table_cols(db_file, "organization_verifications"))
    assert {"document_number", "expires_at"} <= ver_cols

    # Branch row seeded before the upgrade is intact.
    rows = asyncio.run(_branch_rows(db_file))
    assert rows == [(1, 1, "Main Branch")]

    # ── downgrade → additive reverse, legacy data preserved ──
    command.downgrade(cfg, "0015")
    tables = asyncio.run(_table_names(db_file))
    assert not (PHASE7_TABLES & tables), "Phase 7 tables must be gone on downgrade"
    branch_cols = asyncio.run(_table_cols(db_file, "branches"))
    assert "kyc_verified" not in branch_cols
    rows = asyncio.run(_branch_rows(db_file))
    assert rows == [(1, 1, "Main Branch")], (
        "pre-existing branch rows must survive the downgrade unchanged"
    )

    # ── re-upgrade stays idempotent; legacy data still present ──
    command.upgrade(cfg, "0016")
    tables = asyncio.run(_table_names(db_file))
    assert PHASE7_TABLES <= tables
    rows = asyncio.run(_branch_rows(db_file))
    assert rows == [(1, 1, "Main Branch")], (
        "pre-existing branch rows must survive the re-upgrade unchanged"
    )
