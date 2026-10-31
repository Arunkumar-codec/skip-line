"""Branch geo coordinates for discovery distance search (Phase 3 Task 4)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28 12:00:00.000000

Notes:
- Adds nullable `geo_lat` / `geo_lng` columns to `branches` (ARCHITECTURE
  PROPOSAL §data-model). Coordinates are optional: organizations without
  geocoded branches are never returned by radius-filtered discovery but
  remain fully visible to all other filters.
- Purely additive; no backfill required (both columns start NULL).
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("branches") as batch_op:
        batch_op.add_column(sa.Column("geo_lat", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("geo_lng", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("branches") as batch_op:
        batch_op.drop_column("geo_lng")
        batch_op.drop_column("geo_lat")
