"""SCH-004: tokens.booking_source (source-based capacity allocation)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-29 12:00:00.000000

Notes:
- Adds `tokens.booking_source` (nullable=False, server default 'walk_in').
  The issuing channel is set server-side by each booking route
  (kiosk/online/walk-in) and is never trusted from client input (CTR-003).
- Existing rows are backfilled to 'walk_in' by the server default,
  matching Phase 1-2 issuance semantics (day-queue counters).
- Low migration risk: additive column only; no other table touched.
- MySQL execution remains a deployment-gate item (not yet validated).
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tokens",
        sa.Column(
            "booking_source",
            sa.Enum(
                "all", "online", "kiosk", "walk_in", "employee",
                name="tokens_booking_source",
            ),
            nullable=False,
            server_default="walk_in",
        ),
    )


def downgrade() -> None:
    op.drop_column("tokens", "booking_source")
