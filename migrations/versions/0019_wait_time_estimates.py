"""
Phase 10 — Waiting-time estimate snapshots (MASTER §16.6, §21, §23).

Revision ID: 0019
Revises: 0018 (Phase 9 — Workflows + AI)
Created: November 2026

Contents (fully additive, MySQL-8-first, batch-safe):
  new table
    - wait_time_estimates   (append-only predicted-wait snapshots per token:
                             initial prediction at booking + material
                             recalculations after queue events; compared
                             against tokens.called_at for §16.6 accuracy
                             tracking and §21 estimate-accuracy reporting)

Every snapshot keeps its raw estimator inputs (people ahead, active counter
count, duration sample count) and the explainability string so a historical
snapshot remains auditable after the live queue drifts. Predicted values are
nullable: an honest `insufficient_data` estimate is persisted too.

Creation is guarded by _table_exists so hand-stamped databases never
double-create (mirrors 0016/0017/0018). Roundtrip-safe:
upgrade → downgrade → upgrade verified on a disposable database by
tests/unit/test_migration_0019.py (the real project database is NEVER touched).
"""

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def _ts():
    """TimestampMixin-equivalent columns for the new table."""
    return (
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )


def _table_exists(name: str) -> bool:
    from sqlalchemy import inspect

    return name in inspect(op.get_bind()).get_table_names()


def _has_index(conn, table: str, index: str) -> bool:
    try:
        return any(
            i["name"] == index
            for i in sa.inspect(conn).get_indexes(table)
        )
    except Exception:
        return False


def upgrade() -> None:
    if not _table_exists("wait_time_estimates"):
        op.create_table(
            "wait_time_estimates",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("organization_id", sa.Integer(),
                      sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("token_id", sa.Integer(),
                      sa.ForeignKey("tokens.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("counter_id", sa.Integer(),
                      sa.ForeignKey("counters.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("snapshot_kind", sa.String(length=20), nullable=False),
            sa.Column("predicted_wait_minutes", sa.Integer(), nullable=True),
            sa.Column("predicted_range_min", sa.Integer(), nullable=True),
            sa.Column("predicted_range_max", sa.Integer(), nullable=True),
            sa.Column("estimation_status", sa.String(length=30), nullable=False),
            sa.Column("estimation_basis", sa.String(length=30), nullable=False),
            sa.Column("estimation_explanation", sa.String(length=500), nullable=False),
            sa.Column("people_ahead", sa.Integer(), nullable=True),
            sa.Column("active_counter_count", sa.Integer(), nullable=True),
            sa.Column("duration_sample_count", sa.Integer(), nullable=True),
            sa.Column("estimated_at", sa.DateTime(timezone=True),
                      server_default=sa.text("CURRENT_TIMESTAMP"),
                      nullable=False),
            *_ts(),
        )
        conn = op.get_bind()
        if not _has_index(conn, "wait_time_estimates", "ix_wte_token_kind"):
            op.create_index("ix_wte_token_kind", "wait_time_estimates",
                            ["token_id", "snapshot_kind", "id"])
        if not _has_index(conn, "wait_time_estimates", "ix_wte_org_created"):
            op.create_index("ix_wte_org_created", "wait_time_estimates",
                            ["organization_id", "created_at"])
        if not _has_index(conn, "wait_time_estimates", "ix_wait_time_estimates_estimated_at"):
            op.create_index("ix_wait_time_estimates_estimated_at",
                            "wait_time_estimates", ["estimated_at"])


def downgrade() -> None:
    if _table_exists("wait_time_estimates"):
        op.drop_table("wait_time_estimates")
