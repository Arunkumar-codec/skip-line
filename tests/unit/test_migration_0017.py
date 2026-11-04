"""
tests/unit/test_migration_0017.py
─────────────────────────────────
Migration 0017 (`0017_phase8_hardening`) verification on a fully
disposable SQLite database — the real configured database is NEVER
touched (both URL properties are patched, following
test_0016.py / test_0015_schema_sync.py).

Strategy mirrors the 0016 test: a sparse 0016-state schema is
hand-built (only the tables/columns migration 0017 touches), then
`stamp 0016 → upgrade 0017 → downgrade → re-upgrade` runs the real
Alembic chain.

Covers:
- Upgrade creates the five Phase 8 tables, the additive notification /
  scan-evidence columns, and the two hot-path composite indexes.
- A token row existing before the upgrade survives BOTH directions
  (0017 is fully additive, data loss-free).
- Downgrade removes the Phase 8 tables/columns/indexes without losing
  legacy rows.
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

PHASE8_TABLES = {
    "enforcement_actions", "enforcement_appeals",
    "notification_outbox", "notification_preferences", "idempotency_keys",
}

# Minimal 0016-state schema: every table migration 0017 references via
# FK or ALTER, with the columns the seeded rows need.
SPARSE_0016_DDL = (
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, status VARCHAR(30))",
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    "CREATE TABLE counters (id INTEGER PRIMARY KEY)",
    "CREATE TABLE tokens (id INTEGER PRIMARY KEY, organization_id INTEGER, "
    "counter_id INTEGER, service_id INTEGER, status VARCHAR(30), "
    "queue_position INTEGER)",
    "CREATE TABLE notifications (id INTEGER PRIMARY KEY, user_id INTEGER)",
    "CREATE TABLE case_attachments (id INTEGER PRIMARY KEY)",
    "CREATE TABLE organization_verifications (id INTEGER PRIMARY KEY, "
    "organization_id INTEGER)",
)


@pytest.fixture
def migration_env(monkeypatch, tmp_path):
    """A disposable SQLite database used as the Alembic migration target."""
    db_file = tmp_path / "0017_test.db"
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
        rows = await conn.execute("SELECT id, organization_id, status FROM tokens")
        return await rows.fetchall()


async def _build_sparse_0016(db_file):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    async with engine.begin() as conn:
        for ddl in SPARSE_0016_DDL:
            await conn.execute(text(ddl))
        await conn.execute(text(
            "INSERT INTO organizations (id, status) VALUES (1, 'approved')"
        ))
        await conn.execute(text(
            "INSERT INTO tokens (id, organization_id, counter_id, service_id, "
            "status, queue_position) VALUES (1, 1, 1, 1, 'waiting', 1)"
        ))
    await engine.dispose()


def test_0017_roundtrip_preserves_existing_data(migration_env):
    cfg, db_file = migration_env

    asyncio.run(_build_sparse_0016(db_file))
    command.stamp(cfg, "0016")

    # ── upgrade → Phase 8 tables + additive columns + composite indexes ──
    command.upgrade(cfg, "0017")
    tables = asyncio.run(_table_names(db_file))
    assert PHASE8_TABLES <= tables, "all Phase 8 tables must exist after upgrade"

    notif_cols = asyncio.run(_table_cols(db_file, "notifications"))
    assert {"delivery_state", "dedup_key", "template_version"} <= notif_cols
    attach_cols = asyncio.run(_table_cols(db_file, "case_attachments"))
    assert {"scan_status", "scanned_at", "scan_signature"} <= attach_cols
    ver_cols = asyncio.run(_table_cols(db_file, "organization_verifications"))
    assert {"scan_status", "scanned_at", "scan_signature"} <= ver_cols

    token_indexes = asyncio.run(_indexes(db_file, "tokens"))
    assert "ix_tokens_counter_status_pos" in token_indexes
    assert "ix_tokens_org_service_status" in token_indexes

    # Token row seeded before the upgrade is intact.
    rows = asyncio.run(_token_rows(db_file))
    assert rows == [(1, 1, "waiting")]

    # ── downgrade → additive reverse, legacy data preserved ──
    command.downgrade(cfg, "0016")
    tables = asyncio.run(_table_names(db_file))
    assert not (PHASE8_TABLES & tables), "Phase 8 tables must be gone on downgrade"
    notif_cols = asyncio.run(_table_cols(db_file, "notifications"))
    assert "delivery_state" not in notif_cols
    token_indexes = asyncio.run(_indexes(db_file, "tokens"))
    assert "ix_tokens_counter_status_pos" not in token_indexes
    assert "ix_tokens_org_service_status" not in token_indexes
    rows = asyncio.run(_token_rows(db_file))
    assert rows == [(1, 1, "waiting")], (
        "pre-existing token rows must survive the downgrade unchanged"
    )

    # ── re-upgrade stays idempotent; legacy data still present ──
    command.upgrade(cfg, "0017")
    tables = asyncio.run(_table_names(db_file))
    assert PHASE8_TABLES <= tables
    rows = asyncio.run(_token_rows(db_file))
    assert rows == [(1, 1, "waiting")], (
        "pre-existing token rows must survive the re-upgrade unchanged"
    )
