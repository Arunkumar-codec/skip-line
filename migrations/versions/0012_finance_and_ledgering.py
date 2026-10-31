"""Phase 5: Finance & Immutable Ledger (LED-001..004, CASH-001..003, PAY-003..005, FIN-001..004)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-30 22:40:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Alter payments table to add refund_amount and disputed_at
    with op.batch_alter_table("payments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "refund_amount",
                sa.Numeric(10, 2),
                nullable=False,
                server_default="0.00",
            )
        )
        batch_op.add_column(
            sa.Column("disputed_at", sa.DateTime(timezone=True), nullable=True)
        )

    # 2. ledger_entries
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", name="fk_ledger_entries_organization", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("account", sa.String(50), nullable=False, index=True),
        sa.Column("entry_type", sa.String(50), nullable=False, index=True),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("tokens.id", name="fk_ledger_entries_token", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "payment_id",
            sa.Integer(),
            sa.ForeignKey("payments.id", name="fk_ledger_entries_payment", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "counter_shift_id",
            sa.Integer(),
            sa.ForeignKey("counter_shifts.id", name="fk_ledger_entries_counter_shift", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("actor_type", sa.String(20), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("source_transaction_ref", sa.String(100), nullable=True, index=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("entry_metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index("ix_ledger_org_created", "ledger_entries", ["organization_id", "created_at"])
    op.create_index("ix_ledger_account_type", "ledger_entries", ["account", "entry_type"])

    # 3. cash_sessions
    op.create_table(
        "cash_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", name="fk_cash_sessions_organization", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "counter_shift_id",
            sa.Integer(),
            sa.ForeignKey("counter_shifts.id", name="fk_cash_sessions_counter_shift", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "counter_id",
            sa.Integer(),
            sa.ForeignKey("counters.id", name="fk_cash_sessions_counter", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", name="fk_cash_sessions_employee", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("starting_float", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("collected_cash", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("counted_cash", sa.Numeric(10, 2), nullable=True),
        sa.Column("discrepancy", sa.Numeric(10, 2), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="open", index=True),
        sa.Column("denominations", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("verified_by_id", sa.Integer(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index("ix_cash_sessions_org_shift", "cash_sessions", ["organization_id", "counter_shift_id"])

    # 4. refunds
    op.create_table(
        "refunds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", name="fk_refunds_organization", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "payment_id",
            sa.Integer(),
            sa.ForeignKey("payments.id", name="fk_refunds_payment", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("tokens.id", name="fk_refunds_token", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending_approval", index=True),
        sa.Column("gateway_refund_id", sa.String(100), nullable=True, index=True),
        sa.Column("idempotency_key", sa.String(100), nullable=True, unique=True, index=True),
        sa.Column("requested_by_type", sa.String(20), nullable=False),
        sa.Column("requested_by_id", sa.Integer(), nullable=False),
        sa.Column("approved_by_type", sa.String(20), nullable=True),
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    # 5. payouts
    op.create_table(
        "payouts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", name="fk_payouts_organization", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(30), nullable=False, server_default="scheduled", index=True),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column("held_by_type", sa.String(20), nullable=True),
        sa.Column("held_by_id", sa.Integer(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("bank_account_id", sa.Integer(), nullable=True),
        sa.Column("gateway_payout_id", sa.String(100), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    # 6. settlements
    op.create_table(
        "settlements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", name="fk_settlements_organization", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "payout_id",
            sa.Integer(),
            sa.ForeignKey("payouts.id", name="fk_settlements_payout", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("gateway_settlement_id", sa.String(100), nullable=True, index=True),
        sa.Column("gross_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("fee_amount", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("tax_amount", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("net_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("settlement_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending", index=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    # 7. org_bank_accounts
    op.create_table(
        "org_bank_accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", name="fk_org_bank_accounts_org", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("account_number", sa.String(50), nullable=False),
        sa.Column("ifsc_code", sa.String(20), nullable=False),
        sa.Column("account_holder_name", sa.String(150), nullable=False),
        sa.Column("bank_name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending_verification", index=True),
        sa.Column("verification_token", sa.String(100), nullable=True),
        sa.Column("held_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    # 8. financial_risk_flags
    op.create_table(
        "financial_risk_flags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", name="fk_risk_flags_organization", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("risk_type", sa.String(50), nullable=False, index=True),
        sa.Column("severity", sa.String(20), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(30), nullable=False, server_default="flagged", index=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("flag_metadata", sa.JSON(), nullable=True),
        sa.Column("resolved_by", sa.String(100), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    # 9. webhook_events
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(100), nullable=False, unique=True, index=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="processed"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_table("financial_risk_flags")
    op.drop_table("org_bank_accounts")
    op.drop_table("settlements")
    op.drop_table("payouts")
    op.drop_table("refunds")
    op.drop_table("cash_sessions")
    op.drop_table("ledger_entries")

    with op.batch_alter_table("payments") as batch_op:
        batch_op.drop_column("disputed_at")
        batch_op.drop_column("refund_amount")

