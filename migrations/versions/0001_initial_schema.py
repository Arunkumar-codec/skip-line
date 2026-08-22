"""Initial schema — all 12 tables

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # admins
    op.create_table(
        'admins',
        sa.Column('id',            sa.Integer,     primary_key=True, autoincrement=True),
        sa.Column('name',          sa.String(120),  nullable=False),
        sa.Column('email',         sa.String(255),  nullable=False, unique=True),
        sa.Column('password_hash', sa.String(255),  nullable=False),
        sa.Column('is_active',     sa.Boolean,      nullable=False, default=True),
        sa.Column('created_at',    sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at',    sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        mysql_charset='utf8mb4',
    )
    op.create_index('ix_admins_email', 'admins', ['email'], unique=True)

    # organizations
    op.create_table(
        'organizations',
        sa.Column('id',                   sa.Integer,     primary_key=True, autoincrement=True),
        sa.Column('name',                 sa.String(200),  nullable=False),
        sa.Column('registration_number',  sa.String(100),  nullable=False, unique=True),
        sa.Column('org_type',             sa.String(30),   nullable=False),
        sa.Column('address',              sa.Text,         nullable=False),
        sa.Column('city',                 sa.String(100),  nullable=False),
        sa.Column('state',                sa.String(100),  nullable=False),
        sa.Column('email',                sa.String(255),  nullable=False, unique=True),
        sa.Column('mobile',               sa.String(20),   nullable=False),
        sa.Column('password_hash',        sa.String(255),  nullable=False),
        sa.Column('status',               sa.String(20),   nullable=False, default='pending'),
        sa.Column('logo_path',            sa.String(500),  nullable=True),
        sa.Column('description',          sa.Text,         nullable=True),
        sa.Column('is_active',            sa.Boolean,      nullable=False, default=True),
        sa.Column('approved_by_admin_id', sa.Integer,      sa.ForeignKey('admins.id', ondelete='SET NULL'), nullable=True),
        sa.Column('approved_at',          sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at',           sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at',           sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        mysql_charset='utf8mb4',
    )
    op.create_index('ix_orgs_status', 'organizations', ['status'])
    op.create_index('ix_orgs_city',   'organizations', ['city'])
    op.create_index('ix_orgs_email',  'organizations', ['email'], unique=True)

    # users
    op.create_table(
        'users',
        sa.Column('id',            sa.Integer,     primary_key=True, autoincrement=True),
        sa.Column('name',          sa.String(120),  nullable=False),
        sa.Column('email',         sa.String(255),  nullable=False, unique=True),
        sa.Column('mobile',        sa.String(20),   nullable=False),
        sa.Column('address',       sa.Text,         nullable=True),
        sa.Column('password_hash', sa.String(255),  nullable=False),
        sa.Column('is_active',     sa.Boolean,      nullable=False, default=True),
        sa.Column('profile_photo', sa.String(500),  nullable=True),
        sa.Column('created_at',    sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at',    sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        mysql_charset='utf8mb4',
    )
    op.create_index('ix_users_email',  'users', ['email'], unique=True)
    op.create_index('ix_users_mobile', 'users', ['mobile'])

    # services
    op.create_table(
        'services',
        sa.Column('id',                sa.Integer,     primary_key=True, autoincrement=True),
        sa.Column('organization_id',   sa.Integer,     sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name',              sa.String(200),  nullable=False),
        sa.Column('description',       sa.Text,         nullable=True),
        sa.Column('price',             sa.Numeric(10,2), nullable=False, default=0),
        sa.Column('estimated_minutes', sa.Integer,      nullable=False, default=10),
        sa.Column('is_active',         sa.Boolean,      nullable=False, default=True),
        sa.Column('created_at',        sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at',        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        mysql_charset='utf8mb4',
    )

    # counters
    op.create_table(
        'counters',
        sa.Column('id',              sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column('organization_id', sa.Integer,    sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('service_id',      sa.Integer,    sa.ForeignKey('services.id', ondelete='SET NULL'), nullable=True),
        sa.Column('counter_name',    sa.String(100), nullable=False),
        sa.Column('counter_number',  sa.Integer,    nullable=False),
        sa.Column('token_prefix',    sa.String(5),  nullable=False, default='A'),
        sa.Column('daily_limit',     sa.Integer,    nullable=False, default=200),
        sa.Column('status',          sa.String(20), nullable=False, default='active'),
        sa.Column('created_at',      sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at',      sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        mysql_charset='utf8mb4',
    )

    # tokens
    op.create_table(
        'tokens',
        sa.Column('id',              sa.Integer,      primary_key=True, autoincrement=True),
        sa.Column('organization_id', sa.Integer,      sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id',         sa.Integer,      sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('counter_id',      sa.Integer,      sa.ForeignKey('counters.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('service_id',      sa.Integer,      sa.ForeignKey('services.id', ondelete='SET NULL'), nullable=True),
        sa.Column('token_number',    sa.String(20),   nullable=False),
        sa.Column('sequence',        sa.Integer,      nullable=False),
        sa.Column('status',          sa.String(20),   nullable=False, default='waiting'),
        sa.Column('queue_position',  sa.Integer,      nullable=False, default=0),
        sa.Column('guest_name',      sa.String(120),  nullable=True),
        sa.Column('guest_mobile',    sa.String(20),   nullable=True),
        sa.Column('qr_code_path',    sa.String(500),  nullable=True),
        sa.Column('receipt_path',    sa.String(500),  nullable=True),
        sa.Column('amount_paid',     sa.Numeric(10,2), nullable=False, default=0),
        sa.Column('booked_at',       sa.DateTime(timezone=True), nullable=True),
        sa.Column('called_at',       sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at',    sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes',           sa.Text,         nullable=True),
        sa.Column('created_at',      sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at',      sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        mysql_charset='utf8mb4',
    )
    op.create_index('ix_tokens_status',  'tokens', ['status'])
    op.create_index('ix_tokens_org',     'tokens', ['organization_id'])
    op.create_index('ix_tokens_user',    'tokens', ['user_id'])
    op.create_index('ix_tokens_booked',  'tokens', ['booked_at'])

    # token_history, payments, notifications, queue_logs, settings
    op.create_table(
        'token_history',
        sa.Column('id',               sa.Integer,  primary_key=True, autoincrement=True),
        sa.Column('token_id',         sa.Integer,  sa.ForeignKey('tokens.id', ondelete='CASCADE'), nullable=False),
        sa.Column('old_status',       sa.String(30), nullable=True),
        sa.Column('new_status',       sa.String(30), nullable=False),
        sa.Column('changed_by_role',  sa.String(30), nullable=False),
        sa.Column('changed_by_id',    sa.Integer,  nullable=True),
        sa.Column('changed_at',       sa.DateTime(timezone=True), server_default=sa.func.now()),
        mysql_charset='utf8mb4',
    )

    op.create_table(
        'payments',
        sa.Column('id',                  sa.Integer,      primary_key=True, autoincrement=True),
        sa.Column('token_id',            sa.Integer,      sa.ForeignKey('tokens.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('user_id',             sa.Integer,      sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('organization_id',     sa.Integer,      sa.ForeignKey('organizations.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('amount',              sa.Numeric(10,2), nullable=False),
        sa.Column('currency',            sa.String(10),   nullable=False, default='INR'),
        sa.Column('payment_method',      sa.String(20),   nullable=False),
        sa.Column('status',              sa.String(20),   nullable=False, default='pending'),
        sa.Column('razorpay_order_id',   sa.String(100),  nullable=True),
        sa.Column('razorpay_payment_id', sa.String(100),  nullable=True, unique=True),
        sa.Column('razorpay_signature',  sa.String(500),  nullable=True),
        sa.Column('receipt_path',        sa.String(500),  nullable=True),
        sa.Column('paid_at',             sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at',          sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at',          sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        mysql_charset='utf8mb4',
    )

    op.create_table(
        'notifications',
        sa.Column('id',       sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id',  sa.Integer, sa.ForeignKey('users.id',  ondelete='CASCADE'), nullable=False),
        sa.Column('token_id', sa.Integer, sa.ForeignKey('tokens.id', ondelete='SET NULL'), nullable=True),
        sa.Column('type',     sa.String(40), nullable=False),
        sa.Column('channel',  sa.String(20), nullable=False),
        sa.Column('subject',  sa.String(255), nullable=True),
        sa.Column('message',  sa.Text, nullable=False),
        sa.Column('is_read',  sa.Boolean, nullable=False, default=False),
        sa.Column('sent_at',  sa.DateTime(timezone=True), server_default=sa.func.now()),
        mysql_charset='utf8mb4',
    )

    op.create_table(
        'queue_logs',
        sa.Column('id',                sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('organization_id',   sa.Integer, sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('counter_id',        sa.Integer, sa.ForeignKey('counters.id', ondelete='SET NULL'), nullable=True),
        sa.Column('token_id',          sa.Integer, sa.ForeignKey('tokens.id',   ondelete='SET NULL'), nullable=True),
        sa.Column('action',            sa.String(60),  nullable=False),
        sa.Column('performed_by_role', sa.String(30),  nullable=False),
        sa.Column('performed_by_id',   sa.Integer,     nullable=True),
        sa.Column('meta',              sa.String(1000), nullable=True),
        sa.Column('logged_at',         sa.DateTime(timezone=True), server_default=sa.func.now()),
        mysql_charset='utf8mb4',
    )

    op.create_table(
        'settings',
        sa.Column('id',              sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column('organization_id', sa.Integer,    sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('key',             sa.String(100), nullable=False),
        sa.Column('value',           sa.Text,        nullable=True),
        sa.Column('updated_at',      sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.UniqueConstraint('organization_id', 'key', name='uq_org_setting_key'),
        mysql_charset='utf8mb4',
    )

    op.create_table(
        'organization_verifications',
        sa.Column('id',                  sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('organization_id',     sa.Integer, sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('document_type',       sa.String(100), nullable=False),
        sa.Column('document_url',        sa.String(500), nullable=False),
        sa.Column('verification_status', sa.String(20),  nullable=False, default='pending'),
        sa.Column('reviewer_notes',      sa.Text,        nullable=True),
        sa.Column('reviewed_at',         sa.DateTime(timezone=True), nullable=True),
        sa.Column('reviewed_by',         sa.Integer,     sa.ForeignKey('admins.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at',          sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at',          sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        mysql_charset='utf8mb4',
    )


def downgrade() -> None:
    for table in [
        'organization_verifications', 'settings', 'queue_logs',
        'notifications', 'payments', 'token_history', 'tokens',
        'counters', 'services', 'users', 'organizations', 'admins',
    ]:
        op.drop_table(table)
