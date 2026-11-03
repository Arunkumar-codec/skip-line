"""
migrations/versions/0016_trust_feedback_support_kyc.py
─────────────────────────────────────────────────────────────────────
Phase 7 — Trust: Verified Feedback, Unified Support & Organization KYC.

Adds the Phase 7 tables (feedback, feedback_responses, knowledge_solutions,
support_cases, case_participants, case_messages, case_attachments,
organization_kyc) and expands `organization_verifications`
(document_number, expires_at — ORGKYC-001/003) plus `branches`
(kyc_verified / _at / _by — ORGKYC-004 branch-level verification).

Fully additive — no existing data is modified or destroyed. MySQL 8
(InnoDB) is the primary verified target and the SQLite chain test keeps
this file batch-safe. FK names follow the explicit `fk_<table>_<column>`
convention already verified clean under `alembic check` by the 0012-era
table creations; column types match ORM metadata exactly (status columns
are guarded plain Strings; `feedback.moderation_status` is the ORM's
native `moderationstatus` Enum) so no MySQL ENUM ALTER is ever required
and `alembic check` reports zero drift.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _ts():
    """TimestampMixin-equivalent columns for new tables."""
    return (
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )


def _table_exists(name: str) -> bool:
    from sqlalchemy import inspect

    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # ── 1. feedback (FDB-001..005) ───────────────────────────────────────
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token_id", sa.Integer(),
                  sa.ForeignKey("tokens.id", name="fk_feedback_token", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_feedback_user", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("organization_id", sa.Integer(),
                  sa.ForeignKey("organizations.id", name="fk_feedback_organization", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("counter_id", sa.Integer(),
                  sa.ForeignKey("counters.id", name="fk_feedback_counter", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("service_id", sa.Integer(),
                  sa.ForeignKey("services.id", name="fk_feedback_service", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("rating_overall", sa.Integer(), nullable=False),
        sa.Column("rating_waiting", sa.Integer(), nullable=True),
        sa.Column("rating_staff", sa.Integer(), nullable=True),
        sa.Column("rating_service", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(30), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("is_anonymous", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("attachment_path", sa.String(500), nullable=True),
        sa.Column("attachment_name", sa.String(255), nullable=True),
        sa.Column("moderation_status",
                  sa.Enum("pending", "published", "hidden", "reported", "removed",
                          name="moderationstatus"),
                  nullable=False, server_default="pending"),
        sa.Column("moderation_reason", sa.String(500), nullable=True),
        sa.Column("moderated_by", sa.Integer(),
                  sa.ForeignKey("admins.id", name="fk_feedback_moderated_by", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
    )
    op.create_index("ix_feedback_token_id", "feedback", ["token_id"], unique=True)
    op.create_index("ix_feedback_user_id", "feedback", ["user_id"])
    op.create_index("ix_feedback_organization_id", "feedback", ["organization_id"])
    op.create_index("ix_feedback_moderation_status", "feedback", ["moderation_status"])

    op.create_table(
        "feedback_responses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("feedback_id", sa.Integer(),
                  sa.ForeignKey("feedback.id", name="fk_feedback_responses_feedback", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("organization_id", sa.Integer(),
                  sa.ForeignKey("organizations.id", name="fk_feedback_responses_organization", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        *_ts(),
    )
    op.create_index("ix_feedback_responses_feedback_id", "feedback_responses", ["feedback_id"])
    op.create_index("ix_feedback_responses_organization_id", "feedback_responses", ["organization_id"])

    # ── 2. knowledge solutions (KB-001..002) — before case_messages FK ──
    op.create_table(
        "knowledge_solutions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.Integer(),
                  sa.ForeignKey("organizations.id", name="fk_knowledge_solutions_organization",
                                ondelete="CASCADE"),
                  nullable=True),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("audience", sa.String(40), nullable=False, server_default="user"),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by_type", sa.String(20), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("times_used", sa.Integer(), nullable=False, server_default="0"),
        *_ts(),
    )
    op.create_index("ix_knowledge_solutions_organization_id", "knowledge_solutions", ["organization_id"])
    op.create_index("ix_knowledge_solutions_category", "knowledge_solutions", ["category"])

    # ── 3. unified support cases (SUP-001..007, CASE-001..004) ──────────
    op.create_table(
        "support_cases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("reference", sa.String(24), nullable=False),
        sa.Column("case_type", sa.String(30), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="submitted"),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_support_cases_user", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("organization_id", sa.Integer(),
                  sa.ForeignKey("organizations.id", name="fk_support_cases_organization", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("branch_id", sa.Integer(),
                  sa.ForeignKey("branches.id", name="fk_support_cases_branch", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("employee_id", sa.Integer(),
                  sa.ForeignKey("employees.id", name="fk_support_cases_employee", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("counter_id", sa.Integer(),
                  sa.ForeignKey("counters.id", name="fk_support_cases_counter", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("service_id", sa.Integer(),
                  sa.ForeignKey("services.id", name="fk_support_cases_service", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("token_id", sa.Integer(),
                  sa.ForeignKey("tokens.id", name="fk_support_cases_token", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("payment_id", sa.Integer(),
                  sa.ForeignKey("payments.id", name="fk_support_cases_payment", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("feedback_id", sa.Integer(),
                  sa.ForeignKey("feedback.id", name="fk_support_cases_feedback", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("parent_case_id", sa.Integer(),
                  sa.ForeignKey("support_cases.id", name="fk_support_cases_parent", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("assigned_admin_id", sa.Integer(),
                  sa.ForeignKey("admins.id", name="fk_support_cases_assigned_admin", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolution_accepted_by", sa.String(20), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopened_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_type", sa.String(20), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        *_ts(),
    )
    op.create_index("ix_support_cases_reference", "support_cases", ["reference"], unique=True)
    op.create_index("ix_support_cases_case_type", "support_cases", ["case_type"])
    op.create_index("ix_support_cases_status", "support_cases", ["status"])
    op.create_index("ix_support_cases_user_id", "support_cases", ["user_id"])
    op.create_index("ix_support_cases_organization_id", "support_cases", ["organization_id"])
    op.create_index("ix_support_cases_branch_id", "support_cases", ["branch_id"])
    op.create_index("ix_support_cases_token_id", "support_cases", ["token_id"])
    op.create_index("ix_support_cases_payment_id", "support_cases", ["payment_id"])
    op.create_index("ix_support_cases_feedback_id", "support_cases", ["feedback_id"])

    op.create_table(
        "case_participants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.Integer(),
                  sa.ForeignKey("support_cases.id", name="fk_case_participants_case", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("participant_type", sa.String(20), nullable=False),
        sa.Column("participant_id", sa.Integer(), nullable=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        # No DB unique constraint here: the ORM model has none (participant
        # dedup is service-level, e.g. admin_review_participants get-or-verify),
        # and alembic check must stay clean against ORM metadata.
    )
    op.create_index("ix_case_participants_case_id", "case_participants", ["case_id"])

    op.create_table(
        "case_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.Integer(),
                  sa.ForeignKey("support_cases.id", name="fk_case_messages_case", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("sender_type", sa.String(20), nullable=False),
        sa.Column("sender_id", sa.Integer(), nullable=True),
        sa.Column("sender_name", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="parties"),
        sa.Column("review_status", sa.String(20), nullable=False, server_default="sent"),
        sa.Column("suggested_solution_id", sa.Integer(),
                  sa.ForeignKey("knowledge_solutions.id", name="fk_case_messages_suggested_solution",
                                ondelete="SET NULL"),
                  nullable=True),
        *_ts(),
    )
    op.create_index("ix_case_messages_case_id", "case_messages", ["case_id"])

    op.create_table(
        "case_attachments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.Integer(),
                  sa.ForeignKey("support_cases.id", name="fk_case_attachments_case", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("message_id", sa.Integer(),
                  sa.ForeignKey("case_messages.id", name="fk_case_attachments_message", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("uploaded_by_type", sa.String(20), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("stored_path", sa.String(500), nullable=False),
        sa.Column("mime", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        *_ts(),
    )
    op.create_index("ix_case_attachments_case_id", "case_attachments", ["case_id"])

    # ── 4. organization KYC application (ORGKYC-001..007) ────────────────
    op.create_table(
        "organization_kyc",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.Integer(),
                  sa.ForeignKey("organizations.id", name="fk_organization_kyc_organization",
                                ondelete="CASCADE"),
                  nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("tier", sa.String(20), nullable=False, server_default="base"),
        sa.Column("legal_name", sa.String(255), nullable=True),
        sa.Column("registration_number", sa.String(100), nullable=True),
        sa.Column("org_category", sa.String(40), nullable=True),
        sa.Column("registered_address", sa.String(500), nullable=True),
        sa.Column("operating_address", sa.String(500), nullable=True),
        sa.Column("contact_email", sa.String(255), nullable=True),
        sa.Column("contact_phone", sa.String(30), nullable=True),
        sa.Column("representative_name", sa.String(150), nullable=True),
        sa.Column("representative_role", sa.String(100), nullable=True),
        sa.Column("representative_email", sa.String(255), nullable=True),
        sa.Column("representative_phone", sa.String(30), nullable=True),
        sa.Column("representative_verified", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("reviewed_by", sa.Integer(),
                  sa.ForeignKey("admins.id", name="fk_organization_kyc_reviewed_by", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_decision", sa.String(30), nullable=True),
        sa.Column("rejection_reason", sa.String(500), nullable=True),
        sa.Column("more_info_request", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("appeal_status", sa.String(20), nullable=True),
        sa.Column("appeal_reason", sa.Text(), nullable=True),
        sa.Column("appeal_decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("appeal_decision_note", sa.String(500), nullable=True),
        sa.Column("history", sa.JSON(), nullable=True),
        *_ts(),
    )
    op.create_index("ix_organization_kyc_organization_id", "organization_kyc",
                    ["organization_id"], unique=True)
    op.create_index("ix_organization_kyc_status", "organization_kyc", ["status"])

    # ── 5. expand organization_verifications (document identity/expiry) ─
    # Guarded like 0015 does: the table is created by migration 0001; on a
    # sparse 0004-stamped chain (see test_phase3_models) it is absent and
    # the ALTER is skipped — real deployments always take this branch.
    if _table_exists("organization_verifications"):
        op.add_column(
            "organization_verifications",
            sa.Column("document_number", sa.String(100), nullable=True),
        )
        op.add_column(
            "organization_verifications",
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )

    # ── 6. branch-level verification (ORGKYC-004) ───────────────────────
    if _table_exists("branches"):
        op.add_column(
            "branches",
            sa.Column("kyc_verified", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )
        op.add_column(
            "branches",
            sa.Column("kyc_verified_at", sa.DateTime(timezone=True), nullable=True),
        )
        # The FK variant needs `admins` (FK target) AND batch-mode reflection
        # which resolves referenced tables — only run when both exist.
        if _table_exists("admins"):
            with op.batch_alter_table("branches") as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "kyc_verified_by",
                        sa.Integer(),
                        sa.ForeignKey("admins.id", name="fk_branches_kyc_verified_by",
                                      ondelete="SET NULL"),
                        nullable=True,
                    )
                )


def downgrade() -> None:
    # Drop in reverse order. `branches`/`organization_verifications` keep
    # their rows — only the added columns are removed (lossless mirror).
    if _table_exists("branches"):
        if _table_exists("admins"):
            with op.batch_alter_table("branches") as batch_op:
                batch_op.drop_constraint(
                    "fk_branches_kyc_verified_by", type_="foreignkey"
                )
                batch_op.drop_column("kyc_verified_by")
        op.drop_column("branches", "kyc_verified_at")
        op.drop_column("branches", "kyc_verified")
    if _table_exists("organization_verifications"):
        op.drop_column("organization_verifications", "expires_at")
        op.drop_column("organization_verifications", "document_number")
    op.drop_table("organization_kyc")
    op.drop_table("case_attachments")
    op.drop_table("case_messages")
    op.drop_table("case_participants")
    op.drop_table("support_cases")
    op.drop_table("knowledge_solutions")
    op.drop_table("feedback_responses")
    op.drop_table("feedback")
