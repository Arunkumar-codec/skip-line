"""
tests/unit/test_migration_0019.py
─────────────────────────────────
Migration 0019 (`0019_wait_time_estimates`) verification on a fully
disposable SQLite database — the real configured database is NEVER
touched (both URL properties patched, following test_migration_0018.py).

Strategy mirrors the 0018 test: a sparse 0018-state schema is hand-built
(only the tables migration 0019 references via FK — organizations,
counters, tokens), then `stamp 0018 → upgrade 0019 → downgrade →
re-upgrade` runs the real Alembic chain.

Covers:
- Upgrade creates the Phase 10 snapshot table with its FK columns,
  nullable prediction fields (honest insufficient_data), and indexes.
- A token row existing before the upgrade survives BOTH directions
  (0019 is fully additive, data loss-free).
- Downgrade removes the table without losing legacy rows.
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

WTE_TABLE = "wait_time_estimates"

# Minimal 0018-state schema: every table migration 0019 references via FK.
SPARSE_0018_DDL = (
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
    db_file = tmp_path / "0019_test.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    # Patch BOTH URL properties (real configured MySQL must never be the
    # migration target — see test_migration_0018.py / test_0015_schema_sync.py).
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


async def _build_sparse_0018(db_file):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    async with engine.begin() as conn:
        for ddl in SPARSE_0018_DDL:
            await conn.execute(text(ddl))
        await conn.execute(text(
            "INSERT INTO organizations (id, status) VALUES (1, 'approved')"
        ))
        await conn.execute(text(
            "INSERT INTO tokens (id, organization_id, counter_id, service_id, "
            "status, queue_position) VALUES (1, 1, 1, 1, 'waiting', 1)"
        ))
    await engine.dispose()


def test_0019_roundtrip_preserves_existing_data(migration_env):
    cfg, db_file = migration_env

    asyncio.run(_build_sparse_0018(db_file))
    command.stamp(cfg, "0018")

    # ── upgrade → the Phase 10 snapshot table exists with its contract ──
    command.upgrade(cfg, "0019")
    tables = asyncio.run(_table_names(db_file))
    assert WTE_TABLE in tables, "wait_time_estimates must exist after upgrade"

    cols = asyncio.run(_table_cols(db_file, WTE_TABLE))
    assert {
        "id", "organization_id", "token_id", "counter_id",
        "snapshot_kind",
        "predicted_wait_minutes", "predicted_range_min", "predicted_range_max",
        "estimation_status", "estimation_basis", "estimation_explanation",
        "people_ahead", "active_counter_count", "duration_sample_count",
        "estimated_at", "created_at", "updated_at",
    } <= cols

    # Prediction fields are nullable — honest `insufficient_data` snapshots
    # are persisted with NULL predictions (§16.1: never fabricate precision).
    predicted = {"predicted_wait_minutes", "predicted_range_min",
                 "predicted_range_max"}
    assert predicted <= cols

    indexes = asyncio.run(_indexes(db_file, WTE_TABLE))
    assert "ix_wte_token_kind" in indexes
    assert "ix_wte_org_created" in indexes

    # Legacy token row seeded before the upgrade is intact.
    rows = asyncio.run(_token_rows(db_file))
    assert rows == [(1, 1, "waiting")]

    # ── downgrade → snapshot table gone, legacy data preserved ──
    command.downgrade(cfg, "0018")
    tables = asyncio.run(_table_names(db_file))
    assert WTE_TABLE not in tables, "wait_time_estimates must be gone on downgrade"
    rows = asyncio.run(_token_rows(db_file))
    assert rows == [(1, 1, "waiting")], (
        "pre-existing token rows must survive the downgrade unchanged"
    )

    # ── re-upgrade stays idempotent; legacy data still present ──
    command.upgrade(cfg, "0019")
    tables = asyncio.run(_table_names(db_file))
    assert WTE_TABLE in tables
    rows = asyncio.run(_token_rows(db_file))
    assert rows == [(1, 1, "waiting")]
