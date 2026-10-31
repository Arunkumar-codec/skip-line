"""SHIFT-004..006, PHY-001..004, KSK-001..006: shift lifecycle, kiosk devices, walk-in payments

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-30 14:00:00.000000

Notes:
- Extends `counter_shifts` with pause, handover, formal closure and head reconciliation fields (SHIFT-004..006).
- Extends `payments` with `counter_shift_id`, actor details and waiver tracking (PHY-001..004).
- Creates `kiosk_devices` for physical kiosk registration, pairing and heartbeat monitoring (KSK-001..006).
"""

from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Extend counter_shifts ───────────────────────────────────────────────
    with op.batch_alter_table("counter_shifts") as batch_op:
        batch_op.add_column(sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("pause_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column(
                "handover_to_employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="SET NULL", name="fk_counter_shifts_handover_employee"),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("handover_notes", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("closing_cash_counted", sa.Numeric(10, 2), nullable=True))
        batch_op.add_column(sa.Column("closing_cash_expected", sa.Numeric(10, 2), nullable=True))
        batch_op.add_column(sa.Column("closing_cash_variance", sa.Numeric(10, 2), nullable=True))
        batch_op.add_column(
            sa.Column("total_tokens_served", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("total_cash_collected", sa.Numeric(10, 2), nullable=False, server_default="0.00")
        )
        batch_op.add_column(
            sa.Column("total_online_collected", sa.Numeric(10, 2), nullable=False, server_default="0.00")
        )
        batch_op.add_column(
            sa.Column("total_waived_amount", sa.Numeric(10, 2), nullable=False, server_default="0.00")
        )
        batch_op.add_column(sa.Column("closing_remarks", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("reconciled_by_type", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("reconciled_by_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("reconciliation_status", sa.String(30), nullable=True))
        batch_op.add_column(sa.Column("reconciliation_notes", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("adjustment_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("adjustment_amount", sa.Numeric(10, 2), nullable=True))

    # ── 2. Extend payments ────────────────────────────────────────────────────
    with op.batch_alter_table("payments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "counter_shift_id",
                sa.Integer(),
                sa.ForeignKey("counter_shifts.id", ondelete="SET NULL", name="fk_payments_counter_shift"),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("recorded_by_type", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("recorded_by_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("waiver_reason", sa.String(500), nullable=True))
        batch_op.add_column(sa.Column("waiver_approved_by", sa.String(100), nullable=True))
        batch_op.create_index("ix_payments_counter_shift_id", ["counter_shift_id"])

    # ── 3. Create kiosk_devices table ─────────────────────────────────────────
    op.create_table(
        "kiosk_devices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "branch_id",
            sa.Integer(),
            sa.ForeignKey("branches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("device_name", sa.String(100), nullable=False),
        sa.Column("device_code", sa.String(50), nullable=False),
        sa.Column("api_key_hash", sa.String(255), nullable=True),
        sa.Column("pairing_code", sa.String(10), nullable=True),
        sa.Column("pairing_code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("allowed_services", sa.JSON(), nullable=True),
        sa.Column("allowed_counters", sa.JSON(), nullable=True),
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
        sa.UniqueConstraint("device_code"),
    )
    op.create_index("ix_kiosk_devices_organization_id", "kiosk_devices", ["organization_id"])
    op.create_index("ix_kiosk_devices_branch_id", "kiosk_devices", ["branch_id"])
    op.create_index("ix_kiosk_devices_device_code", "kiosk_devices", ["device_code"])
    op.create_index("ix_kiosk_devices_pairing_code", "kiosk_devices", ["pairing_code"])


def downgrade() -> None:
    op.drop_table("kiosk_devices")
    with op.batch_alter_table("payments") as batch_op:
        # MySQL requires the FK to be dropped before an index it depends on
        # (error 1553: "Cannot drop index ...: needed in a foreign key
        # constraint"). Column drops then auto-drop nothing left behind.
        batch_op.drop_constraint("fk_payments_counter_shift", type_="foreignkey")
        batch_op.drop_index("ix_payments_counter_shift_id")
        batch_op.drop_column("waiver_approved_by")
        batch_op.drop_column("waiver_reason")
        batch_op.drop_column("recorded_by_id")
        batch_op.drop_column("recorded_by_type")
        batch_op.drop_column("counter_shift_id")

    with op.batch_alter_table("counter_shifts") as batch_op:
        # MySQL: FK must be dropped before its column (error 1828)
        batch_op.drop_constraint(
            "fk_counter_shifts_handover_employee", type_="foreignkey"
        )
        batch_op.drop_column("adjustment_amount")
        batch_op.drop_column("adjustment_reason")
        batch_op.drop_column("reconciliation_notes")
        batch_op.drop_column("reconciliation_status")
        batch_op.drop_column("reconciled_by_id")
        batch_op.drop_column("reconciled_by_type")
        batch_op.drop_column("reconciled_at")
        batch_op.drop_column("closing_remarks")
        batch_op.drop_column("total_waived_amount")
        batch_op.drop_column("total_online_collected")
        batch_op.drop_column("total_cash_collected")
        batch_op.drop_column("total_tokens_served")
        batch_op.drop_column("closing_cash_variance")
        batch_op.drop_column("closing_cash_expected")
        batch_op.drop_column("closing_cash_counted")
        batch_op.drop_column("closed_at")
        batch_op.drop_column("handover_notes")
        batch_op.drop_column("handover_to_employee_id")
        batch_op.drop_column("resumed_at")
        batch_op.drop_column("pause_reason")
        batch_op.drop_column("paused_at")

