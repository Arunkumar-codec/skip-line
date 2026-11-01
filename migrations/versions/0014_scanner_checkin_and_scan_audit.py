"""QR-003/QR-005: authorized scanner check-in (token_scans + tokens check-in)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-31 21:30:00.000000

Notes:
- `token_scans` is the QR-005 audit trail for authorized scanner check-in
  (one row per scan attempt with result vocabulary from QR-004).
- `tokens.checked_in_at` / `tokens.checked_in_by` mark the first successful
  check-in; NULL = never checked in. Additive and nullable, so every
  pre-existing token (booked before the scanner existed) needs no backfill.
- Forward-only semantics are irrelevant for a mirrored downgrade, but a
  downgrade is still provided for a clean environment rollback.
"""

from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tokens") as batch_op:
        batch_op.add_column(
            sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "checked_in_by",
                sa.Integer(),
                sa.ForeignKey(
                    "employees.id", ondelete="SET NULL", name="fk_tokens_checked_in_by"
                ),
                nullable=True,
            )
        )

    op.create_table(
        "token_scans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey(
                "organizations.id", ondelete="CASCADE", name="fk_scans_organization"
            ),
            nullable=False,
        ),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("tokens.id", ondelete="CASCADE", name="fk_scans_token"),
            nullable=True,
        ),
        sa.Column(
            "employee_id",
            sa.Integer(),
            sa.ForeignKey(
                "employees.id", ondelete="RESTRICT", name="fk_scans_employee"
            ),
            nullable=False,
        ),
        sa.Column(
            "counter_id",
            sa.Integer(),
            sa.ForeignKey("counters.id", ondelete="RESTRICT", name="fk_scans_counter"),
            nullable=True,
        ),
        sa.Column(
            "result",
            sa.Enum(
                "valid",
                "already_checked_in",
                "completed",
                "cancelled",
                "skipped",
                "expired",
                "wrong_branch",
                "wrong_org",
                "tampered",
                "invalid",
                name="scanresult",
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        ),
        sa.Column("device_id", sa.String(120), nullable=True),
        sa.Column(
            "scanned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_token_scans_organization_id", "token_scans", ["organization_id"])
    op.create_index("ix_token_scans_token_id", "token_scans", ["token_id"])
    op.create_index("ix_token_scans_employee_id", "token_scans", ["employee_id"])
    op.create_index(
        "ix_token_scans_org_token", "token_scans", ["organization_id", "token_id"]
    )


def downgrade() -> None:
    # MySQL reuses our ix_* indexes as FK-supporting indexes (error 1553:
    # "Cannot drop index ...: needed in a foreign key constraint"), so the
    # FKs must go before any index/column removal. The whole table is
    # dropped right after, clearing every residual object anyway.
    with op.batch_alter_table("token_scans") as batch_op:
        batch_op.drop_constraint("fk_scans_counter", type_="foreignkey")
        batch_op.drop_constraint("fk_scans_employee", type_="foreignkey")
        batch_op.drop_constraint("fk_scans_organization", type_="foreignkey")
        batch_op.drop_constraint("fk_scans_token", type_="foreignkey")
    op.drop_table("token_scans")

    with op.batch_alter_table("tokens") as batch_op:
        # MySQL: an FK must be dropped before its column (error 1828)
        batch_op.drop_constraint("fk_tokens_checked_in_by", type_="foreignkey")
        batch_op.drop_column("checked_in_by")
        batch_op.drop_column("checked_in_at")
