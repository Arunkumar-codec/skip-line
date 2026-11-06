"""
Phase 9 — Configurable Multi-Stage Workflows + AI Booking groundwork.

Revision ID: 0018
Revises: 0017 (Phase 8 — Hardening)
Created: November 2026

Contents (fully additive, batch-safe on SQLite, MySQL-8-first):
  new tables (MASTER §10 — Workstream B)
    - service_workflows      (one org-configured workflow per service; unconfigured
                              services keep the legacy single-counter flow)
    - workflow_stages        (ordered stages: stable code, mandatory/skippable,
                              estimated minutes, optional counter binding)
    - token_workflow_states  (unique per token; optimistic-CAS stage pointer)
    - token_stage_logs       (append-only stage history with employee identity,
                              reason and point-in-time stage snapshots)
  new tables (MASTER §7/§14/§15 — Workstream C/D)
    - ai_conversations       (manual↔AI booking continuity: mode, context JSON,
                              unconfirmed draft, state status)

Every status-like column is a guarded plain String (no MySQL ENUM ALTERs —
standing Phase-7 decision). Creation is guarded by _has_table so re-running
against databases stamped forward by hand never double-creates, mirroring
0016/0017. Roundtrip-safe: upgrade → downgrade → upgrade verified on a
disposable database by tests/unit/test_migration_0018.py (the real project
database is NEVER touched).
"""

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _ts():
    """TimestampMixin-equivalent columns for new tables."""
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
    # ── 1. service_workflows ────────────────────────────────────────────────
    if not _table_exists("service_workflows"):
        op.create_table(
            "service_workflows",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("organization_id", sa.Integer(),
                      sa.ForeignKey("organizations.id", name="fk_service_workflow_org",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("service_id", sa.Integer(),
                      sa.ForeignKey("services.id", name="fk_service_workflow_service",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            *_ts(),
            sa.UniqueConstraint("service_id", name="uq_service_workflow"),
        )
        op.create_index("ix_service_workflows_organization_id",
                        "service_workflows", ["organization_id"])

    # ── 2. workflow_stages ──────────────────────────────────────────────────
    if not _table_exists("workflow_stages"):
        op.create_table(
            "workflow_stages",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("workflow_id", sa.Integer(),
                      sa.ForeignKey("service_workflows.id", name="fk_workflow_stage_workflow",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("organization_id", sa.Integer(),
                      sa.ForeignKey("organizations.id", name="fk_workflow_stage_org",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("code", sa.String(50), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_mandatory", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("estimated_minutes", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("counter_id", sa.Integer(),
                      sa.ForeignKey("counters.id", name="fk_workflow_stage_counter",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            *_ts(),
            sa.UniqueConstraint("workflow_id", "code", name="uq_workflow_stage_code"),
            sa.UniqueConstraint("workflow_id", "position", name="uq_workflow_stage_position"),
        )
        op.create_index("ix_workflow_stages_workflow_id",
                        "workflow_stages", ["workflow_id"])
        op.create_index("ix_workflow_stages_organization_id",
                        "workflow_stages", ["organization_id"])

    # ── 3. token_workflow_states ────────────────────────────────────────────
    if not _table_exists("token_workflow_states"):
        op.create_table(
            "token_workflow_states",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("token_id", sa.Integer(),
                      sa.ForeignKey("tokens.id", name="fk_token_workflow_state_token",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("organization_id", sa.Integer(),
                      sa.ForeignKey("organizations.id", name="fk_token_workflow_state_org",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("workflow_id", sa.Integer(),
                      sa.ForeignKey("service_workflows.id",
                                    name="fk_token_workflow_state_workflow",
                                    ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("current_stage_id", sa.Integer(),
                      sa.ForeignKey("workflow_stages.id",
                                    name="fk_token_workflow_state_stage",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("status", sa.String(30), nullable=False, server_default="active"),
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("started_at", sa.DateTime(timezone=True),
                      server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
            *_ts(),
            sa.UniqueConstraint("token_id", name="uq_token_workflow_state"),
        )
        op.create_index("ix_token_workflow_states_token_id",
                        "token_workflow_states", ["token_id"])
        op.create_index("ix_token_workflow_states_organization_id",
                        "token_workflow_states", ["organization_id"])

    # ── 4. token_stage_logs ─────────────────────────────────────────────────
    if not _table_exists("token_stage_logs"):
        op.create_table(
            "token_stage_logs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("token_id", sa.Integer(),
                      sa.ForeignKey("tokens.id", name="fk_token_stage_log_token",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("organization_id", sa.Integer(),
                      sa.ForeignKey("organizations.id", name="fk_token_stage_log_org",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("state_id", sa.Integer(),
                      sa.ForeignKey("token_workflow_states.id",
                                    name="fk_token_stage_log_state",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("workflow_id", sa.Integer(), nullable=False),
            sa.Column("action", sa.String(20), nullable=False),
            sa.Column("from_stage_code", sa.String(50), nullable=True),
            sa.Column("to_stage_code", sa.String(50), nullable=True),
            sa.Column("stage_code", sa.String(50), nullable=True),
            sa.Column("stage_name", sa.String(200), nullable=True),
            sa.Column("stage_position", sa.Integer(), nullable=True),
            sa.Column("stage_mandatory", sa.Boolean(), nullable=True),
            sa.Column("employee_id", sa.Integer(),
                      sa.ForeignKey("employees.id", name="fk_token_stage_log_employee",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("employee_name", sa.String(200), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            *_ts(),
        )
        op.create_index("ix_token_stage_logs_token_id",
                        "token_stage_logs", ["token_id"])
        op.create_index("ix_token_stage_logs_organization_id",
                        "token_stage_logs", ["organization_id"])
        op.create_index("ix_token_stage_logs_action",
                        "token_stage_logs", ["action"])

    # ── 5. ai_conversations (Workstream C/D — manual↔AI booking continuity) ─
    if not _table_exists("ai_conversations"):
        op.create_table(
            "ai_conversations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id", name="fk_ai_conversation_user",
                                    ondelete="CASCADE"),
                      nullable=False),
            # manual | ai — the mode the conversation CURRENTLY runs in (D1/D2)
            sa.Column("mode", sa.String(20), nullable=False, server_default="ai"),
            # active | booked | abandoned — D3 guard: booked conversations can
            # never create a second booking through mode switching.
            sa.Column("status", sa.String(30), nullable=False, server_default="active"),
            # Bounded conversation history + collected booking context (C11/D4).
            sa.Column("context_json", sa.Text(), nullable=True),
            sa.Column("draft_json", sa.Text(), nullable=True),
            sa.Column("last_booking_id", sa.Integer(), nullable=True),
            *_ts(),
        )
        op.create_index("ix_ai_conversations_user_id",
                        "ai_conversations", ["user_id"])


def downgrade() -> None:
    # MySQL-8-safe ordering (post-prodcheck finding, Nov 2026): MySQL refuses
    # to drop an index that backs a still-declared FOREIGN KEY (error 1553),
    # so the original index-before-table order — harmless on SQLite, where
    # this was developed and roundtrip-tested — is INVALID on MySQL. The
    # correct order everywhere is: drop the TABLE first, which removes its
    # indexes and FK-enforcing indexes together. No standalone index drops
    # remain; each table's indexes are removed atomically with the table
    # itself. (_has_index is now unused; its removal belongs to the eventual
    # commit review.)
    order = [
        "ai_conversations",
        "token_stage_logs",
        "token_workflow_states",
        "workflow_stages",
        "service_workflows",
    ]
    for tbl in order:
        if _table_exists(tbl):
            op.drop_table(tbl)
