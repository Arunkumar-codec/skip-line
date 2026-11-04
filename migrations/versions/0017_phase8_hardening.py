"""
Phase 8 — Hardening: Enforcement, Notification Reliability & Integrity.

Revision ID: 0017
Revises: 0016 (Phase 7 — Trust)
Created: November 2026

Contents (fully additive, batch-safe on SQLite, MySQL-8-first):
  new tables
    - enforcement_actions       (ENF-002/003/005 escalation ladder + evidence)
    - enforcement_appeals       (ENF-004 organization appeals with decision trail)
    - notification_outbox       (NTF-002 durable deliveries with retry metadata)
    - notification_preferences  (NTF-004 per-user optional-type opt-outs)
    - idempotency_keys          (DB-001 durable mutation idempotency)
  additive columns
    - notifications        +template_version, +dedup_key (+index),
                           +delivery_state (server_default 'delivered')
    - case_attachments     +scan_status, +scanned_at, +scan_signature (SEC-003)
    - organization_verifications +scan_status, +scanned_at, +scan_signature
    - tokens               composite hot-path indexes (see below)
  hot-path composite indexes
    - ix_tokens_counter_status_pos  (counter_id, status, queue_position)
    - ix_tokens_org_service_status  (organization_id, service_id, status)

Every status column is a guarded plain String (no MySQL ENUM ALTERs —
standing Phase-7 decision); new-table enums are stored as their plain
values so `alembic check` shows zero drift against the ORM metadata.
Like 0016, this migration is roundtrip-safe (upgrade → downgrade →
upgrade verified on SQLite AND the disposable MySQL `skipline_synccheck`).
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def _has_table(conn, name: str) -> bool:
    return sa.inspect(conn).has_table(name)


def _table_columns(conn, name: str):
    try:
        return {c["name"] for c in sa.inspect(conn).get_columns(name)}
    except Exception:
        return set()


def _has_index(conn, table: str, index: str) -> bool:
    try:
        return any(
            i["name"] == index
            for i in sa.inspect(conn).get_indexes(table)
        )
    except Exception:
        return False


def upgrade() -> None:
    conn = op.get_bind()

    # ── enforcement_actions ────────────────────────────────────────────────
    if not _has_table(conn, "enforcement_actions"):
        op.create_table(
            "enforcement_actions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("organization_id", sa.Integer(),
                      sa.ForeignKey("organizations.id", name="fk_enforcement_org",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("action_type", sa.String(40), nullable=False),
            sa.Column("status", sa.String(30), nullable=False,
                      server_default="active"),
            sa.Column("scope_type", sa.String(20), nullable=False,
                      server_default="organization"),
            sa.Column("scope_target_id", sa.Integer(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=True),
            sa.Column("correction_deadline", sa.DateTime(timezone=True), nullable=True),
            sa.Column("restricted_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("issued_by_type", sa.String(20), nullable=False,
                      server_default="admin"),
            sa.Column("issued_by_id", sa.Integer(), nullable=True),
            sa.Column("issued_by_label", sa.String(120), nullable=True),
            sa.Column("second_approver_id", sa.Integer(), nullable=True),
            sa.Column("second_approver_label", sa.String(120), nullable=True),
            sa.Column("second_approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("previous_action_id", sa.Integer(),
                      sa.ForeignKey("enforcement_actions.id",
                                    name="fk_enforcement_actions_previous",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("resolved_by_label", sa.String(120), nullable=True),
            sa.Column("resolved_reason", sa.Text(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
        )
        op.create_index("ix_enforcement_actions_organization_id",
                        "enforcement_actions", ["organization_id"])
        op.create_index("ix_enforcement_actions_action_type",
                        "enforcement_actions", ["action_type"])
        op.create_index("ix_enforcement_actions_status",
                        "enforcement_actions", ["status"])

    # ── enforcement_appeals ────────────────────────────────────────────────
    if not _has_table(conn, "enforcement_appeals"):
        op.create_table(
            "enforcement_appeals",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("enforcement_action_id", sa.Integer(),
                      sa.ForeignKey("enforcement_actions.id",
                                    name="fk_enforcement_appeals_action",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("organization_id", sa.Integer(),
                      sa.ForeignKey("organizations.id", name="fk_enforcement_appeals_org",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="submitted"),
            sa.Column("reviewer_label", sa.String(120), nullable=True),
            sa.Column("decision_reason", sa.String(500), nullable=True),
            sa.Column("re_verification_requested", sa.Boolean(), nullable=False,
                      server_default="0"),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
        )
        op.create_index("ix_enforcement_appeals_enforcement_action_id",
                        "enforcement_appeals", ["enforcement_action_id"])
        op.create_index("ix_enforcement_appeals_organization_id",
                        "enforcement_appeals", ["organization_id"])
        op.create_index("ix_enforcement_appeals_status",
                        "enforcement_appeals", ["status"])

    # ── notification_outbox ────────────────────────────────────────────────
    if not _has_table(conn, "notification_outbox"):
        op.create_table(
            "notification_outbox",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("channel", sa.String(20), nullable=False),
            sa.Column("recipient", sa.String(255), nullable=False),
            sa.Column("subject", sa.String(255), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("notification_type", sa.String(40), nullable=False,
                      server_default=""),
            sa.Column("template_version", sa.String(20), nullable=True),
            sa.Column("token_id", sa.Integer(),
                      sa.ForeignKey("tokens.id", name="fk_outbox_token",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("notification_id", sa.Integer(),
                      sa.ForeignKey("notifications.id", name="fk_outbox_notification",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("organization_id", sa.Integer(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("dedup_key", sa.String(255), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("first_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
        )
        op.create_index("ix_notification_outbox_token_id",
                        "notification_outbox", ["token_id"])
        op.create_index("ix_notification_outbox_notification_id",
                        "notification_outbox", ["notification_id"])
        op.create_index("ix_notification_outbox_organization_id",
                        "notification_outbox", ["organization_id"])
        op.create_index("ix_notification_outbox_dedup_key",
                        "notification_outbox", ["dedup_key"])
        op.create_index("ix_notification_outbox_status",
                        "notification_outbox", ["status"])

    # ── notification_preferences ───────────────────────────────────────────
    if not _has_table(conn, "notification_preferences"):
        op.create_table(
            "notification_preferences",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id", name="fk_notification_prefs_user",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("disabled_types", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
        )
        op.create_index("ix_notification_preferences_user_id",
                        "notification_preferences", ["user_id"], unique=True)

    # ── idempotency_keys ───────────────────────────────────────────────────
    if not _has_table(conn, "idempotency_keys"):
        op.create_table(
            "idempotency_keys",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("organization_id", sa.Integer(),
                      sa.ForeignKey("organizations.id", name="fk_idempotency_org",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("key", sa.String(255), nullable=False),
            sa.Column("operation", sa.String(100), nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("response_snapshot", sa.JSON(), nullable=True),
            sa.Column("principal_type", sa.String(20), nullable=True),
            sa.Column("principal_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                      nullable=False),
            sa.UniqueConstraint("organization_id", "key", name="uq_idempotency_keys"),
        )
        op.create_index("ix_idempotency_keys_organization_id",
                        "idempotency_keys", ["organization_id"])
        op.create_index("ix_idempotency_keys_status",
                        "idempotency_keys", ["status"])

    # ── additive columns ───────────────────────────────────────────────────
    # Guarded on TABLE existence as well as column presence: legacy-built
    # chains (pre-notifications baselines) upgrade straight to head.
    if _has_table(conn, "notifications"):
        notif_cols = _table_columns(conn, "notifications")
        if "template_version" not in notif_cols:
            op.add_column("notifications",
                          sa.Column("template_version", sa.String(20), nullable=True))
        if "dedup_key" not in notif_cols:
            op.add_column("notifications",
                          sa.Column("dedup_key", sa.String(255), nullable=True))
            op.create_index("ix_notifications_dedup_key", "notifications", ["dedup_key"])
        if "delivery_state" not in notif_cols:
            op.add_column("notifications",
                          sa.Column("delivery_state", sa.String(20), nullable=False,
                                    server_default="delivered"))
        # schema-sync reconciliation (mirrors the ORM TimestampMixin): some
        # legacy `notifications` tables predate the shared timestamp columns
        for _ts_col in ("created_at", "updated_at"):
            if _ts_col not in notif_cols:
                op.add_column(
                    "notifications",
                    sa.Column(_ts_col, sa.DateTime(timezone=True),
                              server_default=sa.func.now(), nullable=False),
                )

    for table in ("case_attachments", "organization_verifications"):
        if not _has_table(conn, table):
            continue
        cols = _table_columns(conn, table)
        if "scan_status" not in cols:
            op.add_column(table, sa.Column("scan_status", sa.String(20), nullable=True))
        if "scanned_at" not in cols:
            op.add_column(table, sa.Column("scanned_at", sa.DateTime(timezone=True),
                                           nullable=True))
        if "scan_signature" not in cols:
            op.add_column(table, sa.Column("scan_signature", sa.String(120),
                                           nullable=True))

    # ── hot-path composite indexes ─────────────────────────────────────────
    # Guarded on required-column presence too: legacy sparse chains may
    # carry only a partial tokens shape.
    if _has_table(conn, "tokens"):
        tok_cols = _table_columns(conn, "tokens")
        if (
            {"counter_id", "status", "queue_position"} <= tok_cols
            and not _has_index(conn, "tokens", "ix_tokens_counter_status_pos")
        ):
            op.create_index("ix_tokens_counter_status_pos", "tokens",
                            ["counter_id", "status", "queue_position"])
        if (
            {"organization_id", "service_id", "status"} <= tok_cols
            and not _has_index(conn, "tokens", "ix_tokens_org_service_status")
        ):
            op.create_index("ix_tokens_org_service_status", "tokens",
                            ["organization_id", "service_id", "status"])


def downgrade() -> None:
    # ── hot-path composite indexes ─────────────────────────────────────────
    if _has_index(op.get_bind(), "tokens", "ix_tokens_counter_status_pos"):
        op.drop_index("ix_tokens_counter_status_pos", table_name="tokens")
    if _has_index(op.get_bind(), "tokens", "ix_tokens_org_service_status"):
        op.drop_index("ix_tokens_org_service_status", table_name="tokens")

    for table in ("case_attachments", "organization_verifications"):
        cols = _table_columns(op.get_bind(), table)
        if "scan_status" in cols:
            op.drop_column(table, "scan_status")
        if "scanned_at" in cols:
            op.drop_column(table, "scanned_at")
        if "scan_signature" in cols:
            op.drop_column(table, "scan_signature")

    notif_cols = _table_columns(op.get_bind(), "notifications")
    if "delivery_state" in notif_cols:
        op.drop_column("notifications", "delivery_state")
    if "dedup_key" in notif_cols:
        op.drop_index("ix_notifications_dedup_key", table_name="notifications")
        op.drop_column("notifications", "dedup_key")
    if "template_version" in notif_cols:
        op.drop_column("notifications", "template_version")

    if _has_table(op.get_bind(), "idempotency_keys"):
        op.drop_table("idempotency_keys")
    if _has_table(op.get_bind(), "notification_preferences"):
        op.drop_table("notification_preferences")
    if _has_table(op.get_bind(), "notification_outbox"):
        op.drop_table("notification_outbox")
    if _has_table(op.get_bind(), "enforcement_appeals"):
        op.drop_table("enforcement_appeals")
    if _has_table(op.get_bind(), "enforcement_actions"):
        op.drop_table("enforcement_actions")
