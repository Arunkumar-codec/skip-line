"""Create RBAC, sessions, and audit logging tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19 14:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. permissions
    op.create_table(
        "permissions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("module", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_permissions_code", "permissions", ["code"], unique=True)
    op.create_index("ix_permissions_module", "permissions", ["module"])

    # 2. roles
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("is_system", sa.Boolean, nullable=False, default=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("organization_id", "code", name="uq_roles_org_code"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_roles_org_id", "roles", ["organization_id"])
    op.create_index("ix_roles_code", "roles", ["code"])

    # 3. role_permissions
    op.create_table(
        "role_permissions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "role_id",
            sa.Integer,
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "permission_id",
            sa.Integer,
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_role_permissions_role_id", "role_permissions", ["role_id"])
    op.create_index("ix_role_permissions_perm_id", "role_permissions", ["permission_id"])

    # 4. employee_role_assignments
    op.create_table(
        "employee_role_assignments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "employee_id",
            sa.Integer,
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.Integer,
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "branch_id",
            sa.Integer,
            sa.ForeignKey("branches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assigned_by_org_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("employee_id", "role_id", "branch_id", name="uq_employee_role_branch"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_emp_role_emp_id", "employee_role_assignments", ["employee_id"])
    op.create_index("ix_emp_role_role_id", "employee_role_assignments", ["role_id"])
    op.create_index("ix_emp_role_branch_id", "employee_role_assignments", ["branch_id"])

    # 5. sessions
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("jti", sa.String(64), nullable=False, unique=True),
        sa.Column("principal_type", sa.String(32), nullable=False),
        sa.Column("principal_id", sa.Integer, nullable=False),
        sa.Column("organization_id", sa.Integer, nullable=True),
        sa.Column("token_type", sa.String(32), nullable=False, default="access"),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_revoked", sa.Boolean, nullable=False, default=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_active_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_sessions_jti", "sessions", ["jti"], unique=True)
    op.create_index("ix_sessions_principal", "sessions", ["principal_type", "principal_id"])
    op.create_index("ix_sessions_org_id", "sessions", ["organization_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_index("ix_sessions_is_revoked", "sessions", ["is_revoked"])

    # 6. audit_logs
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("principal_type", sa.String(32), nullable=False),
        sa.Column("principal_id", sa.Integer, nullable=True),
        sa.Column("principal_email", sa.String(255), nullable=True),
        sa.Column("organization_id", sa.Integer, nullable=True),
        sa.Column("branch_id", sa.Integer, nullable=True),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, default="SUCCESS"),
        sa.Column("details", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_principal_type", "audit_logs", ["principal_type"])
    op.create_index("ix_audit_logs_principal_id", "audit_logs", ["principal_id"])
    op.create_index("ix_audit_logs_org_id", "audit_logs", ["organization_id"])
    op.create_index("ix_audit_logs_target", "audit_logs", ["target_type", "target_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("sessions")
    op.drop_table("employee_role_assignments")
    op.drop_table("role_permissions")
    op.drop_table("roles")
    op.drop_table("permissions")
