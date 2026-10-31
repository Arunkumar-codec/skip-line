"""Phase 4 completion: kiosk/walk-in booking idempotency (KSK-005) and supervised waiver evidence (PHY-003)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30 16:00:00.000000

Notes:
- `tokens.client_reference`: channel-supplied idempotency reference so a retried
  kiosk or assisted booking (e.g. after a printer failure) returns the SAME
  token instead of issuing a duplicate.
- `payments.waiver_evidence`: supporting-evidence reference recorded with
  supervisor-approved fee waivers (PHY-003).
"""

from alembic import op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tokens") as batch_op:
        batch_op.add_column(sa.Column("client_reference", sa.String(64), nullable=True))
        batch_op.create_index("ix_tokens_client_reference", ["client_reference"])

    with op.batch_alter_table("payments") as batch_op:
        batch_op.add_column(sa.Column("waiver_evidence", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("payments") as batch_op:
        batch_op.drop_column("waiver_evidence")

    with op.batch_alter_table("tokens") as batch_op:
        batch_op.drop_index("ix_tokens_client_reference")
        batch_op.drop_column("client_reference")
