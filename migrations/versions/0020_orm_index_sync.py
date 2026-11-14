"""
Final-closure verification fix — ORM/chain index sync for the workflow tables.

Revision ID: 0020
Revises: 0019 (Phase 10 — Waiting-Time Estimates)
Created: November 14, 2026

Found by the local-verification pass (Task 1): on a FRESHLY chain-built
database (0001→0019, disposable MySQL), `alembic check` reported drift
between migration 0018's DDL and the ORM metadata that is the source of
truth:

  1. token_stage_logs.state_id   — ORM declares index=True; 0018 created
     indexes for token_id/organization_id/action but NOT state_id.
  2. token_workflow_states.token_id — ORM declares unique=True + the
     uq_token_workflow_state constraint; 0018 created a PLAIN (non-unique)
     ix_token_workflow_states_token_id. Functionally significant: a
     non-unique index would allow duplicate per-token stage-pointer rows
     that the CAS workflow logic assumes can never exist.

Fix (guarded, idempotent, data loss-free, additive-only):
  upgrade: ensure ix_token_stage_logs_state_id exists; recreate
           ix_token_workflow_states_token_id as UNIQUE (only if it exists
           and is non-unique — never touches a DB already in the right
           shape, e.g. create_all-built ones).
  downgrade: restore the exact 0018 shape (token_id index back to
           non-unique; state_id index RENAMED back to the FK constraint
           name on MySQL — 1553-safe, same index object) so the roundtrip
           is faithful.

Style mirrors 0015/0018: inspect-before-touch, MySQL-8-first, batch-safe
on SQLite (drop/create index), never assumes an index exists. The real
project databases are NEVER touched by this file; verification runs on
disposable databases only.
"""

from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

STATE_LOGS_INDEX = "ix_token_stage_logs_state_id"
TOKEN_STATE_INDEX = "ix_token_workflow_states_token_id"


def _table_exists(name: str) -> bool:
    from sqlalchemy import inspect

    return name in inspect(op.get_bind()).get_table_names()


def _index_of(conn, table: str, name: str):
    for info in sa.inspect(conn).get_indexes(table):
        if info["name"] == name:
            return info
    return None


def _ensure_index(table: str, name: str, column: str, unique: bool) -> None:
    conn = op.get_bind()
    if not _table_exists(table):
        return
    existing = _index_of(conn, table, name)
    if existing is not None and bool(existing.get("unique")) == unique:
        return  # already in the target shape
    if existing is not None:
        op.drop_index(name, table_name=table)
    op.create_index(name, table, [column], unique=unique)


def upgrade() -> None:
    # 1. token_stage_logs.state_id — ORM index=True; missing from 0018 DDL.
    _ensure_index("token_stage_logs", STATE_LOGS_INDEX, "state_id", unique=False)
    # 2. token_workflow_states.token_id — ORM unique=True; 0018 created it
    #    non-unique. Recreate as UNIQUE (drop + create, MySQL-8-safe).
    _ensure_index("token_workflow_states", TOKEN_STATE_INDEX, "token_id", unique=True)


def downgrade() -> None:
    # Restore the exact 0018 shape so upgrade→downgrade→upgrade is faithful.
    conn = op.get_bind()
    dialect = conn.dialect.name
    if _table_exists("token_workflow_states"):
        info = _index_of(conn, "token_workflow_states", TOKEN_STATE_INDEX)
        if info and info.get("unique"):
            # FKs on token_id remain backed by uq_token_workflow_state, so a
            # plain drop + recreate is safe here (no MySQL 1553 risk).
            op.drop_index(TOKEN_STATE_INDEX, table_name="token_workflow_states")
            op.create_index(TOKEN_STATE_INDEX, "token_workflow_states", ["token_id"])
    if _table_exists("token_stage_logs"):
        if _index_of(conn, "token_stage_logs", STATE_LOGS_INDEX) is None:
            return
        fk_backing = _index_of(conn, "token_stage_logs", "fk_token_stage_log_state")
        if dialect == "mysql" and fk_backing is None:
            # MySQL 1553-safe (0015 pattern): the FK was reassigned to
            # ix_token_stage_logs_state_id when 0018's implicit index was
            # superseded, so RENAME it back to the FK constraint name — the
            # exact implicit shape a fresh 0018 chain build produces.
            op.execute("ALTER TABLE token_stage_logs RENAME INDEX "
                       "`ix_token_stage_logs_state_id` TO `fk_token_stage_log_state`")
        else:
            # SQLite has no RENAME INDEX and no FK-index requirement;
            # MySQL keeps the FK on the surviving fk-named index here.
            op.drop_index(STATE_LOGS_INDEX, table_name="token_stage_logs")
