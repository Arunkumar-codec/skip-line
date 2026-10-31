"""SCH-007: schedule_change_previews (Decision 11 impact workflow)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29 12:00:00.000000

Notes:
- New table `schedule_change_previews` persists the schedule-change
  impact workflow (SCH-007 / OPEN_DECISIONS Decision 11): proposal,
  computed impact, server-side stale-preview fingerprint, per-booking
  resolution decisions, and the executed outcome summary.
- Purely additive: no existing table or column changes, no data
  backfill. Ordinary schedule CRUD (Decision 17) is untouched.
- `notifications.type` already is a plain VARCHAR(40) in MySQL
  (migration 0001), so the new `schedule_change` notification type used
  by the workflow requires no ALTER TABLE.
- MySQL execution remains a deployment-gate item (not yet validated).
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule_change_previews",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "entity",
            sa.Enum(
                "weekly_schedule", "schedule_exception", "capacity_rule",
                name="schedule_change_entity",
            ),
            nullable=False,
        ),
        sa.Column(
            "edit_type",
            sa.Enum("create", "update", "delete", name="schedule_change_edit_type"),
            nullable=False,
        ),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "confirmed", "rejected", "stale", "expired",
                name="schedule_change_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("proposal", sa.JSON(), nullable=False),
        sa.Column("impact", sa.JSON(), nullable=False),
        sa.Column("decisions", sa.JSON(), nullable=True),
        sa.Column("impact_digest", sa.String(64), nullable=False),
        sa.Column("affected_counter_ids", sa.JSON(), nullable=False),
        sa.Column("horizon_start", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_summary", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("target_id >= 0", name="ck_schedule_change_target_id"),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_schedule_change_previews_organization_id",
        "schedule_change_previews",
        ["organization_id"],
    )
    op.create_index(
        "ix_schedule_change_previews_status",
        "schedule_change_previews",
        ["status"],
    )
    op.create_index(
        "ix_schedule_change_org_status",
        "schedule_change_previews",
        ["organization_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("schedule_change_previews")
