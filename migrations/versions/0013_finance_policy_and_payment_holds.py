"""FIN-001: Payment-method policy & paid-booking pause (organizations + tokens)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-31 20:30:00.000000

Notes:
- `organizations.allowed_payment_methods` is a nullable JSON array whose
  values come from the `PaymentMethod` enum vocabulary (razorpay, upi,
  cash, waived, free, pay_at_counter). NULL = unrestricted (backward
  compatible with every pre-existing organization).
- `tokens.payment_hold` marks an admin-initiated FIN-001 pause of a PAID
  booking. Pausing reuses the existing `TokenStatus.ON_HOLD` queue/display
  semantics (capacity stays consumed — Open Decision 16); the operator's
  previous status is stored in `payment_hold_previous_status` so resume
  restores the exact queue position state. These columns are additive and
  nullable/defaulted, so existing tokens need no backfill.
"""

from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(sa.Column("allowed_payment_methods", sa.JSON(), nullable=True))

    with op.batch_alter_table("tokens") as batch_op:
        batch_op.add_column(
            sa.Column(
                "payment_hold",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column("payment_hold_reason", sa.String(500), nullable=True)
        )
        batch_op.add_column(
            sa.Column("payment_hold_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("payment_hold_by", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "payment_hold_previous_status",
                sa.String(32),
                nullable=True,
            )
        )


def downgrade() -> None:
    # MySQL: drop dependents before the flag column they document.
    with op.batch_alter_table("tokens") as batch_op:
        batch_op.drop_column("payment_hold_previous_status")
        batch_op.drop_column("payment_hold_by")
        batch_op.drop_column("payment_hold_at")
        batch_op.drop_column("payment_hold_reason")
        batch_op.drop_column("payment_hold")

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_column("allowed_payment_methods")
