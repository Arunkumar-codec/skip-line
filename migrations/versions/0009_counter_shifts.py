"""SHIFT-001/002: daily counter shift state machine

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-29 12:00:00.000000

Notes:
- Creates `counter_shifts` (SHIFT-001/002): daily counter operational
  shifts with the lifecycle
  OPEN_REQUESTED -> APPROVED -> ACTIVE (later: PAUSED/CLOSE_REQUESTED/
  RECONCILING/CLOSED). A rejected request stays OPEN_REQUESTED with the
  head's recorded reason (no separate REJECTED enum value; see
  OPEN_DECISIONS "Shift workflow notes").
- `counter_id` / `employee_id` use RESTRICT so counters and employees
  with shift history cannot be silently deleted (integrity parity with
  `tokens.counter_id`).
- The `(organization_id, shift_date, status)` composite index backs the
  head's request queue and daily shift listings.
- Opening shift status is decided by the application; no server default
  is set on `status`.
- MySQL execution remains a deployment-gate item (not yet validated).
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "counter_shifts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "counter_id",
            sa.Integer(),
            sa.ForeignKey("counters.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("shift_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "closed",
                "open_requested",
                "approved",
                "active",
                "paused",
                "close_requested",
                "reconciling",
                name="counter_shifts_status",
            ),
            nullable=False,
        ),
        sa.Column("requested_services", sa.JSON(), nullable=False),
        sa.Column("device_ready", sa.Boolean(), nullable=False),
        sa.Column("opening_cash", sa.Numeric(10, 2), nullable=False),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_type", sa.String(20), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("decided_reason", sa.Text(), nullable=True),
        sa.Column("approved_services", sa.JSON(), nullable=True),
        sa.Column("approved_period_start", sa.Time(), nullable=True),
        sa.Column("approved_period_end", sa.Time(), nullable=True),
        sa.Column("payment_permitted", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_counter_shifts_organization_id", "counter_shifts", ["organization_id"])
    op.create_index("ix_counter_shifts_counter_id", "counter_shifts", ["counter_id"])
    op.create_index("ix_counter_shifts_employee_id", "counter_shifts", ["employee_id"])
    op.create_index(
        "ix_shifts_org_date_status", "counter_shifts", ["organization_id", "shift_date", "status"]
    )


def downgrade() -> None:
    # MySQL: explicit drop_index calls fail here (error 1553) because the
    # counter_shifts foreign keys still require those indexes. Dropping the
    # table removes all of its indexes and constraints atomically.
    op.drop_table("counter_shifts")
