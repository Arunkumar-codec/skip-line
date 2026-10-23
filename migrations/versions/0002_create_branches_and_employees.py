"""Create branches and employees tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # branches
    op.create_table(
        "branches",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("branch_code", sa.String(50), nullable=False),
        sa.Column("address", sa.Text, nullable=False),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("state", sa.String(100), nullable=False),
        sa.Column("pincode", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("organization_id", "branch_code", name="uq_org_branch_code"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_branches_org_id", "branches", ["organization_id"])
    op.create_index("ix_branches_branch_code", "branches", ["branch_code"])
    op.create_index("ix_branches_city", "branches", ["city"])
    op.create_index("ix_branches_state", "branches", ["state"])

    # employees
    op.create_table(
        "employees",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "branch_id",
            sa.Integer,
            sa.ForeignKey("branches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("employee_code", sa.String(50), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("organization_id", "employee_code", name="uq_org_employee_code"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_employees_org_id", "employees", ["organization_id"])
    op.create_index("ix_employees_branch_id", "employees", ["branch_id"])
    op.create_index("ix_employees_email", "employees", ["email"], unique=True)
    op.create_index("ix_employees_employee_code", "employees", ["employee_code"])


def downgrade() -> None:
    op.drop_table("employees")
    op.drop_table("branches")
