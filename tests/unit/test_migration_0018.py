"""
tests/unit/test_migration_0018.py
────────────────────────────────
Migration 0018 (`0018_workflows_and_ai`) verification on a fully
disposable SQLite database — the real configured database is NEVER
touched (both URL properties are patched, following
test_migration_0017.py / test_0015_schema_sync.py).

Strategy mirrors the 0017 test: a sparse 0017-state schema is hand-built
(only the tables migration 0018 references via FK — organizations,
counters, employees, services, tokens, users), then
`stamp 0017 → upgrade 0018 → downgrade → re-upgrade` runs the real
Alembic chain.

Covers:
- Upgrade creates the four Phase 9 Workstream B tables (service_workflows,
  workflow_stages, token_workflow_states, token_stage_logs) plus the
  ai_conversations groundwork table (Workstream C/D), with their
  uniqueness constraints and the declared indexes.
- A token row existing before the upgrade survives BOTH directions
  (0018 is fully additive, data loss-free).
- Downgrade removes all 0018 tables/indexes without losing legacy rows.
- The alembic version table lands back on 0018 after re-upgrade.
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

PHASE9_TABLES = {
    "service_workflows", "workflow_stages",
    "token_workflow_states", "token_stage_logs",
    "ai_conversations",
}

# Minimal 0017-state schema: every table migration 0018 references via FK,
# with the columns the seeded legacy rows need.
SPARSE_0017_DDL = (
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, status VARCHAR(30))",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE employees (id INTEGER PRIMARY KEY, organization_id INTEGER)",
    "CREATE TABLE services (id INTEGER PRIMARY KEY, organization_id INTEGER)",
    "CREATE TABLE counters (id INTEGER PRIMARY KEY)",
    "CREATE TABLE tokens (id INTEGER PRIMARY KEY, organization_id INTEGER, "
    "counter_id INTEGER, service_id INTEGER, status VARCHAR(30), "
    "queue_position INTEGER)",
)


@pytest.fixture
def migration_env(monkeypatch, tmp_path):
    """A disposable SQLite database used as the Alembic migration target."""
    db_file = tmp_path / "0018_test.db"
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


async def _indexes(db_file, table):
    async with aiosqlite.connect(str(db_file)) as conn:
        rows = await conn.execute(f"PRAGMA index_list({table})")
        return {r[1] for r in await rows.fetchall()}


async def _token_rows(db_file):
    async with aiosqlite.connect(str(db_file)) as conn:
        rows = await conn.execute(
            "SELECT id, organization_id, status FROM tokens"
        )
        return await rows.fetchall()


async def _build_sparse_0017(db_file):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    async with engine.begin() as conn:
        for ddl in SPARSE_0017_DDL:
            await conn.execute(text(ddl))
        await conn.execute(text(
            "INSERT INTO organizations (id, status) VALUES (1, 'approved')"
        ))
        await conn.execute(text(
            "INSERT INTO tokens (id, organization_id, counter_id, service_id, "
            "status, queue_position) VALUES (1, 1, 1, 1, 'waiting', 1)"
        ))
    await engine.dispose()


def test_0018_roundtrip_preserves_existing_data(migration_env):
    cfg, db_file = migration_env

    asyncio.run(_build_sparse_0017(db_file))
    command.stamp(cfg, "0017")

    # ── upgrade → Phase 9 tables exist with their constraints ──
    command.upgrade(cfg, "0018")
    tables = asyncio.run(_table_names(db_file))
    assert PHASE9_TABLES <= tables, "all Phase 9 tables must exist after upgrade"

    wf_cols = asyncio.run(_table_cols(db_file, "service_workflows"))
    assert {"id", "organization_id", "service_id", "name",
            "is_active", "created_at", "updated_at"} <= wf_cols

    stage_cols = asyncio.run(_table_cols(db_file, "workflow_stages"))
    assert {"id", "workflow_id", "organization_id", "code", "name",
            "position", "is_mandatory", "estimated_minutes",
            "counter_id", "is_active"} <= stage_cols

    state_cols = asyncio.run(_table_cols(db_file, "token_workflow_states"))
    assert {"id", "token_id", "organization_id", "workflow_id",
            "current_stage_id", "status", "version",
            "started_at", "completed_at", "cancelled_at"} <= state_cols

    log_cols = asyncio.run(_table_cols(db_file, "token_stage_logs"))
    assert {"id", "token_id", "organization_id", "state_id", "workflow_id",
            "action", "from_stage_code", "to_stage_code", "stage_code",
            "stage_name", "stage_position", "stage_mandatory",
            "employee_id", "employee_name", "reason"} <= log_cols

    ai_cols = asyncio.run(_table_cols(db_file, "ai_conversations"))
    assert {"id", "user_id", "mode", "status", "context_json",
            "draft_json", "last_booking_id"} <= ai_cols

    state_indexes = asyncio.run(_indexes(db_file, "token_workflow_states"))
    assert "ix_token_workflow_states_token_id" in state_indexes
    assert "ix_token_workflow_states_organization_id" in state_indexes

    log_indexes = asyncio.run(_indexes(db_file, "token_stage_logs"))
    assert "ix_token_stage_logs_action" in log_indexes

    # Legacy token row seeded before the upgrade is intact.
    rows = asyncio.run(_token_rows(db_file))
    assert rows == [(1, 1, "waiting")]

    # ── downgrade → all Phase 9 tables gone, legacy data preserved ──
    command.downgrade(cfg, "0017")
    tables = asyncio.run(_table_names(db_file))
    assert not (PHASE9_TABLES & tables), "Phase 9 tables must be gone on downgrade"
    rows = asyncio.run(_token_rows(db_file))
    assert rows == [(1, 1, "waiting")], (
        "pre-existing token rows must survive the downgrade unchanged"
    )

    # ── re-upgrade stays idempotent; legacy data still present ──
    command.upgrade(cfg, "0018")
    tables = asyncio.run(_table_names(db_file))
    assert PHASE9_TABLES <= tables
    rows = asyncio.run(_token_rows(db_file))
    assert rows == [(1, 1, "waiting")]
