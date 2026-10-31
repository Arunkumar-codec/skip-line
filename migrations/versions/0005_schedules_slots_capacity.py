"""Availability: Schedules, Exceptions, Slots, Capacity Rules & Timezones

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28 12:00:00.000000

Notes:
- `weekly_schedules`, `schedule_exceptions`, `capacity_rules` use a
  polymorphic `(scope_level, scope_id)` scope instead of nullable FK
  columns; their uniqueness constraints include `organization_id`
  (tenant-aware: `scope_id` values are only unique within a tenant, so
  two organizations' org-level rows would otherwise collide). All three
  constraint designs behave identically on MySQL and SQLite.
- `time_slots` is created BEFORE `tokens.slot_id` is added, because
  MySQL requires the FK target table to exist.
- `organizations.timezone` is NOT NULL with a server default
  (`Asia/Kolkata`, Decision 12) so the single UPDATE-free backfill is
  safe on existing rows.
- `tokens.slot_id` is nullable (SET NULL on slot deletion); existing
  day-queue tokens remain valid with `slot_id = NULL`.
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Organizations: IANA timezone (SCH-009, Decision 12)
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "timezone",
                sa.String(64),
                nullable=False,
                server_default="Asia/Kolkata",
            )
        )

    # 2. Branches: optional IANA timezone override (NULL = inherit org)
    with op.batch_alter_table("branches") as batch_op:
        batch_op.add_column(
            sa.Column("timezone", sa.String(64), nullable=True)
        )

    # 3. Counters: booking capacity mode (Decision 10; default preserves
    #    all Phase 1-2 day-queue behavior)
    with op.batch_alter_table("counters") as batch_op:
        batch_op.add_column(
            sa.Column(
                "queue_mode",
                sa.Enum("day_queue", "slots", name="queuemode"),
                nullable=False,
                server_default="day_queue",
            )
        )

    # 4. Create time_slots (before tokens.slot_id — FK target must exist)
    op.create_table(
        "time_slots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "counter_id",
            sa.Integer(),
            sa.ForeignKey("counters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("services.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("slot_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("booked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("held_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
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
        sa.UniqueConstraint(
            "counter_id", "slot_date", "start_time", name="uq_time_slot_counter_date_start"
        ),
        sa.CheckConstraint("capacity >= 0", name="ck_time_slot_capacity_nonnegative"),
        sa.CheckConstraint("booked_count >= 0", name="ck_time_slot_booked_nonnegative"),
        sa.CheckConstraint("held_count >= 0", name="ck_time_slot_held_nonnegative"),
        sa.CheckConstraint(
            "booked_count + held_count <= capacity", name="ck_time_slot_within_capacity"
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_time_slots_organization_id", "time_slots", ["organization_id"])
    op.create_index("ix_time_slots_counter_id", "time_slots", ["counter_id"])
    op.create_index("ix_time_slots_service_id", "time_slots", ["service_id"])
    op.create_index("ix_time_slots_slot_date", "time_slots", ["slot_date"])

    # 5. Create weekly_schedules
    op.create_table(
        "weekly_schedules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scope_level",
            sa.Enum("org", "branch", "counter", "service", name="schedule_scope_level"),
            nullable=False,
            server_default="org",
        ),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("open_time", sa.Time(), nullable=True),
        sa.Column("close_time", sa.Time(), nullable=True),
        sa.Column("slot_interval_minutes", sa.Integer(), nullable=True),
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
        sa.UniqueConstraint(
            "organization_id", "scope_level", "scope_id", "day_of_week",
            name="uq_weekly_schedule_org_scope_day"
        ),
        sa.CheckConstraint(
            "day_of_week >= 0 AND day_of_week <= 6", name="ck_weekly_schedule_dow"
        ),
        sa.CheckConstraint(
            "is_closed = 1 OR (open_time IS NOT NULL AND close_time IS NOT NULL "
            "AND open_time < close_time)",
            name="ck_weekly_schedule_hours",
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_weekly_schedules_organization_id", "weekly_schedules", ["organization_id"])
    op.create_index("ix_weekly_schedules_scope_level", "weekly_schedules", ["scope_level"])
    op.create_index("ix_weekly_schedules_scope_id", "weekly_schedules", ["scope_id"])

    # 6. Create schedule_exceptions
    op.create_table(
        "schedule_exceptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scope_level",
            sa.Enum("org", "branch", "counter", "service", name="schedule_scope_level"),
            nullable=False,
            server_default="org",
        ),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("exception_date", sa.Date(), nullable=False),
        sa.Column(
            "exception_type",
            sa.Enum(
                "holiday", "emergency_closure", "maintenance", "special_hours",
                "reduced_capacity", "counter_closure", "service_closure",
                name="schedule_exception_type",
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("opens_at", sa.Time(), nullable=True),
        sa.Column("closes_at", sa.Time(), nullable=True),
        sa.Column("capacity_factor", sa.Numeric(3, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
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
        sa.UniqueConstraint(
            "organization_id", "scope_level", "scope_id", "exception_date",
            name="uq_schedule_exception_org_scope_date"
        ),
        sa.CheckConstraint(
            "capacity_factor IS NULL OR (capacity_factor > 0 AND capacity_factor <= 1)",
            name="ck_schedule_exception_capacity_factor",
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_schedule_exceptions_organization_id", "schedule_exceptions", ["organization_id"]
    )
    op.create_index(
        "ix_schedule_exceptions_exception_date", "schedule_exceptions", ["exception_date"]
    )
    op.create_index("ix_schedule_exceptions_scope_level", "schedule_exceptions", ["scope_level"])
    op.create_index("ix_schedule_exceptions_scope_id", "schedule_exceptions", ["scope_id"])

    # 7. Create capacity_rules
    op.create_table(
        "capacity_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scope_level",
            sa.Enum("org", "branch", "counter", "service", name="schedule_scope_level"),
            nullable=False,
            server_default="org",
        ),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column(
            "limit_type",
            sa.Enum(
                "daily_token", "slot_capacity", "walk_in_quota",
                "per_user", "priority_reserve",
                name="capacity_limit_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "booking_source",
            sa.Enum("all", "online", "kiosk", "walk_in", "employee", name="booking_source"),
            nullable=False,
            server_default="all",
        ),
        sa.Column("max_count", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
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
        sa.UniqueConstraint(
            "organization_id", "scope_level", "scope_id", "limit_type", "booking_source",
            name="uq_capacity_rule_org_scope_type_source"
        ),
        sa.CheckConstraint("max_count >= 0", name="ck_capacity_rule_max_count"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_capacity_rules_organization_id", "capacity_rules", ["organization_id"])
    op.create_index("ix_capacity_rules_scope_level", "capacity_rules", ["scope_level"])
    op.create_index("ix_capacity_rules_scope_id", "capacity_rules", ["scope_id"])
    op.create_index("ix_capacity_rules_limit_type", "capacity_rules", ["limit_type"])

    # 8. Tokens: nullable slot reference (last — FK target exists now)
    with op.batch_alter_table("tokens") as batch_op:
        batch_op.add_column(
            sa.Column(
                "slot_id",
                sa.Integer(),
                sa.ForeignKey(
                    "time_slots.id", ondelete="SET NULL", name="fk_tokens_slot_id"
                ),
                nullable=True,
            )
        )
        batch_op.create_index("ix_tokens_slot_id", ["slot_id"])


def downgrade() -> None:
    with op.batch_alter_table("tokens") as batch_op:
        # MySQL: FK must be dropped before an index it depends on
        # (error 1553: "Cannot drop index ...: needed in a foreign key constraint")
        batch_op.drop_constraint("fk_tokens_slot_id", type_="foreignkey")
        batch_op.drop_index("ix_tokens_slot_id")
        batch_op.drop_column("slot_id")

    op.drop_table("capacity_rules")
    op.drop_table("schedule_exceptions")
    op.drop_table("weekly_schedules")
    op.drop_table("time_slots")

    with op.batch_alter_table("counters") as batch_op:
        batch_op.drop_column("queue_mode")

    with op.batch_alter_table("branches") as batch_op:
        batch_op.drop_column("timezone")

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_column("timezone")
