"""Catalogue, Multi-Service Counters, Sub-services, Custom Forms & Identity Verification

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Update services (branch_id, service_code, requires_identity_verification)
    with op.batch_alter_table("services") as batch_op:
        batch_op.add_column(
            sa.Column(
                "branch_id",
                sa.Integer(),
                sa.ForeignKey("branches.id", ondelete="SET NULL", name="fk_services_branch"),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("service_code", sa.String(50), nullable=True))
        batch_op.add_column(
            sa.Column(
                "requires_identity_verification",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.create_index("ix_services_branch_id", ["branch_id"])
        batch_op.create_index("ix_services_service_code", ["service_code"])

    # 2. Update counters (branch_id)
    with op.batch_alter_table("counters") as batch_op:
        batch_op.add_column(
            sa.Column(
                "branch_id",
                sa.Integer(),
                sa.ForeignKey("branches.id", ondelete="SET NULL", name="fk_counters_branch"),
                nullable=True,
            )
        )
        batch_op.create_index("ix_counters_branch_id", ["branch_id"])

    # 3. Create counter_services
    op.create_table(
        "counter_services",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "counter_id",
            sa.Integer(),
            sa.ForeignKey("counters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("counter_id", "service_id", name="uq_counter_service"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_counter_services_counter_id", "counter_services", ["counter_id"])
    op.create_index("ix_counter_services_service_id", "counter_services", ["service_id"])

    # Backfill existing counters.service_id into counter_services
    op.execute(
        """
        INSERT INTO counter_services (counter_id, service_id, is_primary)
        SELECT id, service_id, 1
        FROM counters
        WHERE service_id IS NOT NULL
        """
    )

    # 4. Create sub_services
    op.create_table(
        "sub_services",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "additional_price",
            sa.Numeric(10, 2),
            server_default="0.00",
            nullable=False,
        ),
        sa.Column(
            "additional_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
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
        sa.UniqueConstraint("service_id", "code", name="uq_service_subservice_code"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_sub_services_service_id", "sub_services", ["service_id"])
    op.create_index("ix_sub_services_code", "sub_services", ["code"])

    # 5. Create custom_forms
    op.create_table(
        "custom_forms",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("services.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_published", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
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
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_custom_forms_organization_id", "custom_forms", ["organization_id"])
    op.create_index("ix_custom_forms_service_id", "custom_forms", ["service_id"])

    # 6. Create form_fields
    op.create_table(
        "form_fields",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "form_id",
            sa.Integer(),
            sa.ForeignKey("custom_forms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_key", sa.String(100), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("field_type", sa.String(50), nullable=False),
        sa.Column("placeholder", sa.String(200), nullable=True),
        sa.Column("help_text", sa.Text(), nullable=True),
        sa.Column("is_required", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("validation_regex", sa.String(255), nullable=True),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
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
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_form_fields_form_id", "form_fields", ["form_id"])
    op.create_index("ix_form_fields_field_key", "form_fields", ["field_key"])

    # 7. Create form_field_options
    op.create_table(
        "form_field_options",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "field_id",
            sa.Integer(),
            sa.ForeignKey("form_fields.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("option_label", sa.String(200), nullable=False),
        sa.Column("option_value", sa.String(200), nullable=False),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_form_field_options_field_id", "form_field_options", ["field_id"])

    # 8. Create form_field_conditions
    op.create_table(
        "form_field_conditions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "field_id",
            sa.Integer(),
            sa.ForeignKey("form_fields.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "depends_on_field_id",
            sa.Integer(),
            sa.ForeignKey("form_fields.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operator", sa.String(50), nullable=False),
        sa.Column("trigger_value", sa.String(255), nullable=False),
        sa.Column("action", sa.String(50), server_default="show", nullable=False),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_form_field_conditions_field_id", "form_field_conditions", ["field_id"])
    op.create_index(
        "ix_form_field_conditions_depends_on",
        "form_field_conditions",
        ["depends_on_field_id"],
    )

    # 9. Create token_form_snapshots
    op.create_table(
        "token_form_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("tokens.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "form_id",
            sa.Integer(),
            sa.ForeignKey("custom_forms.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("form_title", sa.String(200), nullable=False),
        sa.Column("form_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("schema_snapshot", sa.Text(), nullable=False),
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
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_token_form_snapshots_token_id", "token_form_snapshots", ["token_id"])
    op.create_index("ix_token_form_snapshots_form_id", "token_form_snapshots", ["form_id"])

    # 10. Create token_form_answers
    op.create_table(
        "token_form_answers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("token_form_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_key", sa.String(100), nullable=False),
        sa.Column("field_label", sa.String(200), nullable=False),
        sa.Column("field_type", sa.String(50), nullable=False),
        sa.Column("answer_value", sa.Text(), nullable=True),
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
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_token_form_answers_snapshot_id", "token_form_answers", ["snapshot_id"])
    op.create_index("ix_token_form_answers_field_key", "token_form_answers", ["field_key"])

    # 11. Create user_identities
    op.create_table(
        "user_identities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("id_type", sa.String(50), nullable=False),
        sa.Column("encrypted_id_number", sa.Text(), nullable=False),
        sa.Column("masked_id_number", sa.String(50), nullable=False),
        sa.Column("document_path", sa.String(500), nullable=True),
        sa.Column("status", sa.String(50), server_default="pending", nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_by_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])
    op.create_index("ix_user_identities_organization_id", "user_identities", ["organization_id"])
    op.create_index("ix_user_identities_id_type", "user_identities", ["id_type"])
    op.create_index("ix_user_identities_status", "user_identities", ["status"])

    # 12. Create token_services
    op.create_table(
        "token_services",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("services.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "sub_service_id",
            sa.Integer(),
            sa.ForeignKey("sub_services.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("price", sa.Numeric(10, 2), server_default="0.00", nullable=False),
        sa.Column("estimated_minutes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sequence_order", sa.Integer(), server_default="1", nullable=False),
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
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_token_services_token_id", "token_services", ["token_id"])
    op.create_index("ix_token_services_service_id", "token_services", ["service_id"])
    op.create_index("ix_token_services_sub_service_id", "token_services", ["sub_service_id"])


def downgrade() -> None:
    op.drop_table("token_services")
    op.drop_table("user_identities")
    op.drop_table("token_form_answers")
    op.drop_table("token_form_snapshots")
    op.drop_table("form_field_conditions")
    op.drop_table("form_field_options")
    op.drop_table("form_fields")
    op.drop_table("custom_forms")
    op.drop_table("sub_services")
    op.drop_table("counter_services")

    with op.batch_alter_table("counters") as batch_op:
        # MySQL: FK must be dropped before an index it depends on (error 1553)
        batch_op.drop_constraint("fk_counters_branch", type_="foreignkey")
        batch_op.drop_index("ix_counters_branch_id")
        batch_op.drop_column("branch_id")

    with op.batch_alter_table("services") as batch_op:
        batch_op.drop_constraint("fk_services_branch", type_="foreignkey")
        batch_op.drop_index("ix_services_service_code")
        batch_op.drop_index("ix_services_branch_id")
        batch_op.drop_column("requires_identity_verification")
        batch_op.drop_column("service_code")
        batch_op.drop_column("branch_id")
