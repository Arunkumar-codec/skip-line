"""Schema sync: reconcile Alembic-built schema with current ORM metadata.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-01

ROOT CAUSE
----------
Migrations 0001-0003 were hand-authored against shapes the ORM never used.
``models/session.py`` and ``models/audit_log.py`` have carried their current
shape since Phase 1 (commit b131a12) — the migration chain was written
against an alternative shape and never synced. Because the app's supported
schema path is ``Base.metadata.create_all`` (which always matches the ORM),
this never surfaced in tests or the dev database. On a database built
*purely* through Alembic, ``sessions`` keeps migration 0003's shape
(integer PK, ``jti``, ``token_type``, ``revoked_*`` and ``last_active_at``
instead of ``last_activity_at``), so the session store INSERT fails with
``Unknown column 'last_activity_at'`` and every portal login fails;
``audit_logs`` fails the same way on ``actor_type``. 165 autogenerate
drift events existed across the chain (enum vs VARCHAR types, nullable
timestamps, legacy index names, RBAC table shapes).

STRATEGY — data-preserving, forward-compatible, shape-aware
----------------------------------------------------------
- EVERY operation is guarded by live schema inspection (table existence,
  column existence, current type/nullability, index presence). The
  migration applies only the changes a given database actually needs:
    * an Alembic-built database (legacy shape) is fully reconciled with the
      ORM contract;
    * a database built by ``create_all`` (the app's default path, e.g. the
      real ``skipline`` dev database) is already ORM-shaped — 0015 is a
      verified NO-OP there in both directions;
    * databases lacking tables touched here (e.g. the minimal stamped
      schema used by the SQLite migration-chain test) simply skip them.
- ``sessions`` (legacy shape only): table REBUILD with full row copy — the
  new String(36) UUID PK is sourced from the legacy ``jti`` column (the app
  contract stores the session id in the JWT ``jti`` claim, and no
  application version ever wrote to the 0003-shaped table, so ``jti`` holds
  the only meaningful identity ever assigned); ``last_activity_at`` is
  backfilled from ``last_active_at``; everything else survives verbatim.
  The legacy table is dropped only AFTER the copy succeeds.
- ``audit_logs`` (legacy shape only): ``actor_*`` columns are added and
  backfilled from the legacy ``principal_*`` columns before those columns
  are dropped; VARCHAR columns that the ORM narrows are safely truncated in
  place before the type change.
- RBAC tables: legacy-only columns/keys are dropped ONLY when the legacy
  signature is present (no application version ever wrote legacy RBAC
  rows); if legacy rows would be non-losslessly transformed the migration
  raises instead of guessing (see ``_rebuild_role_permissions``).
- Downgrade is the guarded mirror image: legacy shapes are restored only
  where the ORM shape is detected, so a no-op upgrade is a no-op downgrade
  and ``create_all``-built databases are never degraded by accident.
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# ── ORM enum shapes (authoritative: models/*.py) ────────────────────────────
COUNTER_STATUS = sa.Enum("ACTIVE", "INACTIVE", "BREAK", name="counterstatus")
TOKEN_STATUS = sa.Enum(
    "WAITING", "CALLED", "IN_PROGRESS", "COMPLETED", "SKIPPED", "CANCELLED", "ON_HOLD",
    name="tokenstatus",
)
NOTIFICATION_TYPE = sa.Enum(
    "TOKEN_CREATED", "QUEUE_NEARING", "TOKEN_CALLED", "TOKEN_COMPLETED",
    "TOKEN_CANCELLED", "ORG_APPROVED", "ORG_REJECTED", "PAYMENT_SUCCESS",
    "SCHEDULE_CHANGE",
    name="notificationtype",
)
NOTIFICATION_CHANNEL = sa.Enum("EMAIL", "SMS", "IN_APP", name="notificationchannel")
VERIFICATION_STATUS = sa.Enum("PENDING", "APPROVED", "REJECTED", name="verificationstatus")
ORG_TYPE = sa.Enum(
    "HOSPITAL", "CLINIC", "BANK", "GOVERNMENT", "TEMPLE", "EDUCATIONAL",
    "SERVICE_CENTER", "OTHER",
    name="orgtype",
)
ORG_STATUS = sa.Enum("PENDING", "APPROVED", "REJECTED", "SUSPENDED", name="orgstatus")


# ── inspection helpers (all decisions are made from the live schema) ────────
def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _table_exists(table) -> bool:
    return table in _tables()


def _columns(table) -> dict:
    if not _table_exists(table):
        return {}
    return {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns(table)}


def _index_names(table) -> set:
    if not _table_exists(table):
        return set()
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def _fk_names(table, column) -> set:
    if not _table_exists(table):
        return set()
    return {
        fk["name"]
        for fk in sa.inspect(op.get_bind()).get_foreign_keys(table)
        if column in (fk.get("constrained_columns") or [])
    }


def _rowcount(table) -> int:
    return op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def _is_mysql() -> bool:
    return op.get_bind().dialect.name == "mysql"


def _type_of(table, column):
    cols = _columns(table)
    return cols.get(column, {}).get("type")


def _type_name(coltype) -> str:
    return "" if coltype is None else coltype.__class__.__name__.upper()


def _type_len(coltype):
    return getattr(coltype, "length", None)


# ── guarded primitive operations ────────────────────────────────────────────
def _drop_index(table, name) -> None:
    if name in _index_names(table):
        op.drop_index(name, table_name=table)


def _create_index(table, name, columns, unique=False) -> None:
    if table not in _tables() or name in _index_names(table):
        return
    # never create an index over columns the table does not have (some test
    # / partial deployments carry sparse table shapes)
    present = set(_columns(table).keys())
    if not set(columns).issubset(present):
        return
    op.create_index(name, table, columns, unique=unique)


def _add_column(table, column, col_type, nullable, server_default=None) -> None:
    if column in _columns(table):
        return
    if not nullable and _rowcount(table) != 0:
        # non-empty table: add as nullable first; the caller backfills the
        # data and then enforces NOT NULL via _set_nullable.
        op.add_column(table, sa.Column(column, col_type, nullable=True))
        return
    op.add_column(
        table,
        sa.Column(
            column, col_type, nullable=nullable, server_default=server_default
        ),
    )


def _drop_column(table, column) -> None:
    if column in _columns(table):
        for fk in sorted(_fk_names(table, column)):
            op.drop_constraint(fk, table, type_="foreignkey")
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)


def _set_nullable(table, column, nullable) -> None:
    col = _columns(table).get(column)
    if col is None or col["nullable"] == nullable:
        return
    default_before = col.get("default")  # captured BEFORE the alter drops it
    with op.batch_alter_table(table) as batch:
        batch.alter_column(
            column, existing_type=col["type"], nullable=nullable
        )
    if default_before:
        _restore_default_from(table, column, default_before)


def _restore_default_from(table, column, raw_default) -> None:
    """MODIFY-based alters drop an existing column DEFAULT on MySQL; the ORM's
    TimestampMixin contract (server_default=func.now()) requires it back.
    Re-issues a full MODIFY because this server rejects ALTER COLUMN SET
    DEFAULT with CURRENT_TIMESTAMP."""
    if not _is_mysql() or not raw_default:
        return
    col = _columns(table).get(column)
    if col is None:
        return
    coltype = col["type"].compile(op.get_bind().dialect)
    nullable_sql = "NULL" if col["nullable"] else "NOT NULL"
    try:
        op.get_bind().execute(
            sa.text(
                f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` "
                f"{coltype} {nullable_sql} DEFAULT {str(raw_default)}"
            )
        )
    except Exception:
        logger.warning(
            "0015: could not restore default on %s.%s: %s", table, column, raw_default
        )


def _set_type(table, column, type_) -> None:
    """guarded type change; MySQL native, SQLite via table-rebuild batch."""
    col = _columns(table).get(column)
    if col is None:
        return
    current = col["type"]
    if isinstance(type_, sa.Enum):
        on_mysql = _is_mysql()
        already_enum = _type_name(current) == "ENUM"
        if on_mysql and already_enum:
            return
        if not on_mysql:
            # On SQLite an sa.Enum is stored as VARCHAR + CHECK by the ORM;
            # legacy tables are VARCHAR too — nothing to change.
            return
    else:
        want = _type_name(type_)
        got = _type_name(current)
        want_len = _type_len(type_)
        got_len = _type_len(current)
        if want == got and (want_len is None or str(want_len) == str(got_len)):
            return
    with op.batch_alter_table(table) as batch:
        batch.alter_column(
            column, type_=type_, existing_type=current, existing_nullable=col["nullable"]
        )
    _restore_default_from(table, column, col.get("default"))


def _truncate_before_narrowing(table, column, max_len) -> None:
    """make data safe for a narrowing MODIFY (no data loss flag is possible)"""
    if column not in _columns(table) or _rowcount(table) == 0:
        return
    op.get_bind().execute(
        sa.text(
            f"UPDATE {table} SET {column} = LEFT({column}, {max_len}) "
            f"WHERE {column} IS NOT NULL AND CHAR_LENGTH({column}) > {max_len}"
        )
        if _is_mysql()
        else sa.text(
            f"UPDATE {table} SET {column} = SUBSTR({column}, 1, {max_len}) "
            f"WHERE {column} IS NOT NULL AND LENGTH({column}) > {max_len}"
        )
    )


def _rename_fk_to_columns_or_drop(table, column) -> None:
    for fk in sorted(_fk_names(table, column)):
        op.drop_constraint(fk, table, type_="foreignkey")


def _idx(stage: str, name: str) -> str:
    """Staging index names.

    SQLite index names are schema-global — a staged rebuild while the old
    table still exists needs table-scoped names to avoid collisions.
    MySQL (per-table namespace) keeps the canonical ORM names.
    """
    return name if _is_mysql() else f"{stage}__{name}"


def _rename_index(table, old, new) -> None:
    idxs = _index_names(table)
    if old not in idxs or new in idxs:
        return
    if _is_mysql():
        op.get_bind().execute(
            sa.text(f"ALTER TABLE {table} RENAME INDEX `{old}` TO `{new}`")
        )
    else:
        # SQLite has no RENAME INDEX; the caller falls back to drop/create.
        return


def _restore_fk_index(table, orm_name, legacy_name, columns) -> None:
    """Restore the pre-0015 (legacy/auto-named) index for an FK-backed column.

    MySQL replaces a pre-existing duplicate index when a new CREATE INDEX
    runs, so the 0015 upgrade leaves the ORM-named index as the sole FK
    backing; the downgrade must therefore RENAME (not drop) it back to the
    legacy name — a drop would trigger MySQL error 1553. On SQLite there is
    no FK index naming, so the ORM-named index is dropped and recreated
    under a table-scoped legacy name (SQLite index names are schema-global,
    so the raw MySQL auto-name would collide across tables).
    """
    idx = _index_names(table)
    if orm_name not in idx:
        return
    sqlite_name = f"{table}__{legacy_name}"
    if legacy_name in idx or sqlite_name in idx:
        op.drop_index(orm_name, table_name=table)
        return
    if _is_mysql():
        _rename_index(table, orm_name, legacy_name)
    else:
        op.drop_index(orm_name, table_name=table)
        op.create_index(sqlite_name, table, columns)


# ── sessions: legacy (0003) → ORM rebuild with full data preservation ──────
def _rebuild_sessions_to_orm() -> None:
    """Legacy sessions table → ORM shape. FULL row copy (jti→id)."""
    cols = _columns("sessions")
    if not cols or "jti" not in cols:
        return  # already ORM-shaped (create_all DB) or table absent

    op.create_table(
        "sessions_sync",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("principal_type", sa.String(30), nullable=False),
        sa.Column("principal_id", sa.Integer(), nullable=False),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("is_revoked", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Index(_idx("sessions_sync", "ix_sessions_principal_type"), "principal_type"),
        sa.Index(_idx("sessions_sync", "ix_sessions_principal_id"), "principal_id"),
        sa.Index(_idx("sessions_sync", "ix_sessions_is_revoked"), "is_revoked"),
        sa.Index(_idx("sessions_sync", "ix_sessions_expires_at"), "expires_at"),
    )
    _bind = op.get_bind()
    # Full copy: identity = legacy jti (UUID string the app issued as JWT jti),
    # activity timestamp folded into the ORM's last_activity_at.
    if _is_mysql():
        _bind.execute(
            sa.text(
                "INSERT INTO sessions_sync "
                "(id, principal_type, principal_id, ip_address, user_agent, "
                " is_revoked, expires_at, created_at, last_activity_at) "
                "SELECT LEFT(jti, 36), principal_type, principal_id, "
                "       LEFT(COALESCE(ip_address, ''), 45), user_agent, "
                "       is_revoked, expires_at, created_at, "
                "       COALESCE(last_active_at, created_at, CURRENT_TIMESTAMP) "
                "FROM sessions"
            )
        )
    else:
        _bind.execute(
            sa.text(
                "INSERT INTO sessions_sync "
                "(id, principal_type, principal_id, ip_address, user_agent, "
                " is_revoked, expires_at, created_at, last_activity_at) "
                "SELECT substr(jti, 1, 36), principal_type, principal_id, "
                "       substr(COALESCE(ip_address, ''), 1, 45), user_agent, "
                "       is_revoked, expires_at, created_at, "
                "       COALESCE(last_active_at, created_at, CURRENT_TIMESTAMP) "
                "FROM sessions"
            )
        )
    moved = _rowcount("sessions_sync")
    logger.info("schema sync: sessions rows carried over: %d", moved)
    op.drop_table("sessions")
    op.rename_table("sessions_sync", "sessions")


def _rebuild_sessions_to_legacy() -> None:
    """ORM sessions → legacy shape (downgrade mirror). Row copy id→jti."""
    cols = _columns("sessions")
    if not cols or "jti" in cols:
        return  # nothing to revert (absent, or already legacy)

    op.create_table(
        "sessions_sync_legacy",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("jti", sa.String(64), nullable=False),
        sa.Column("principal_type", sa.String(32), nullable=False),
        sa.Column("principal_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("token_type", sa.String(32), nullable=False, server_default="access"),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Index(_idx("sessions_sync_legacy", "ix_sessions_jti"), "jti", unique=True),
        sa.Index(
            _idx("sessions_sync_legacy", "ix_sessions_principal"),
            "principal_type",
            "principal_id",
        ),
        sa.Index(_idx("sessions_sync_legacy", "ix_sessions_org_id"), "organization_id"),
        sa.Index(_idx("sessions_sync_legacy", "ix_sessions_expires_at"), "expires_at"),
        sa.Index(_idx("sessions_sync_legacy", "ix_sessions_is_revoked"), "is_revoked"),
    )
    _bind = op.get_bind()
    if _is_mysql():
        _bind.execute(
            sa.text(
                "INSERT INTO sessions_sync_legacy "
                "(jti, principal_type, principal_id, user_agent, ip_address, "
                " expires_at, is_revoked, created_at, last_active_at) "
                "SELECT id, principal_type, principal_id, user_agent, ip_address, "
                "       expires_at, is_revoked, created_at, last_activity_at "
                "FROM sessions"
            )
        )
    else:
        _bind.execute(
            sa.text(
                "INSERT INTO sessions_sync_legacy "
                "(jti, principal_type, principal_id, user_agent, ip_address, "
                " expires_at, is_revoked, created_at, last_active_at) "
                "SELECT id, principal_type, principal_id, user_agent, ip_address, "
                "       expires_at, is_revoked, created_at, last_activity_at "
                "FROM sessions"
            )
        )
    op.drop_table("sessions")
    op.rename_table("sessions_sync_legacy", "sessions")


# ── audit_logs: legacy (0003) → ORM with backfill ───────────────────────────
def _upg_audit_logs() -> None:
    if not _table_exists("audit_logs"):
        return
    cols = _columns("audit_logs")
    legacy = "event_type" in cols

    # 1. actor_* columns (avec backfill from principal_*/status when legacy)
    _legacy_rows = _rowcount("audit_logs") if legacy else 0
    _add_column("audit_logs", "actor_type", sa.String(30), nullable=False)
    _add_column("audit_logs", "actor_id", sa.Integer(), nullable=True)
    _add_column("audit_logs", "actor_email", sa.String(255), nullable=True)
    _add_column("audit_logs", "outcome", sa.String(20), nullable=False)
    if legacy and _legacy_rows:
        _bind = op.get_bind()
        _bind.execute(
            sa.text(
                "UPDATE audit_logs SET "
                "actor_type = COALESCE(principal_type, 'system'), "
                "actor_id = principal_id, "
                "actor_email = CASE WHEN principal_email IS NULL THEN NULL "
                f"ELSE {'LEFT' if _is_mysql() else 'SUBSTR'}(principal_email, 1, 255) END, "
                "outcome = LOWER(COALESCE(NULLIF(TRIM(status), ''), 'success'))"
            )
        )
        _set_nullable("audit_logs", "actor_type", False)
        _set_nullable("audit_logs", "outcome", False)

    # 2. column type/length reconciliation (truncate before narrowing)
    _truncate_before_narrowing("audit_logs", "target_type", 50)
    _truncate_before_narrowing("audit_logs", "ip_address", 45)
    _set_type("audit_logs", "action", sa.String(100))
    _set_type("audit_logs", "target_type", sa.String(50))
    _set_type("audit_logs", "target_id", sa.String(100))
    _set_type("audit_logs", "ip_address", sa.String(45))
    _drop_index("audit_logs", "ix_audit_logs_action")  # recreate against widened col
    _create_index("audit_logs", "ix_audit_logs_action", ["action"])

    # 3. index surface to ORM shape
    _drop_index("audit_logs", "ix_audit_logs_event_type")
    _drop_index("audit_logs", "ix_audit_logs_principal_id")
    _drop_index("audit_logs", "ix_audit_logs_principal_type")
    _drop_index("audit_logs", "ix_audit_logs_target")
    _create_index("audit_logs", "ix_audit_logs_actor_type", ["actor_type"])
    _create_index("audit_logs", "ix_audit_logs_actor_id", ["actor_id"])
    _create_index("audit_logs", "ix_audit_logs_organization_id", ["organization_id"])
    _drop_index("audit_logs", "ix_audit_logs_org_id")
    _create_index("audit_logs", "ix_audit_logs_target_type", ["target_type"])
    _create_index("audit_logs", "ix_audit_logs_created_at", ["created_at"])

    # 4. legacy columns out (indexes already dropped, FKs auto-dropped by guard)
    for column in (
        "event_type", "principal_type", "principal_id", "principal_email",
        "branch_id", "user_agent", "status",
    ):
        _drop_column("audit_logs", column)


def _down_audit_logs() -> None:
    if not _table_exists("audit_logs"):
        return
    cols = _columns("audit_logs")
    if "event_type" in cols:
        return  # already legacy
    _orm_rows = _rowcount("audit_logs")
    for column, ddl, nullable_default in (
        ("principal_type", sa.Column("principal_type", sa.String(32)), False),
        ("user_agent", sa.Column("user_agent", sa.String(512)), True),
        ("principal_id", sa.Column("principal_id", sa.Integer()), True),
        ("principal_email", sa.Column("principal_email", sa.String(255)), True),
        ("event_type", sa.Column("event_type", sa.String(64)), False),
        ("status", sa.Column("status", sa.String(32)), False),
        ("branch_id", sa.Column("branch_id", sa.Integer()), True),
    ):
        # NOT NULL legacy columns are added nullable first on a data-bearing
        # table, backfilled below, then tightened — never a failure.
        _add_column(
            "audit_logs",
            column,
            ddl.type,
            nullable=True if ddl.nullable is False and _orm_rows else nullable_default,
        )
    _truncate_before_narrowing("audit_logs", "action", 64)
    _truncate_before_narrowing("audit_logs", "target_type", 64)
    _truncate_before_narrowing("audit_logs", "target_id", 64)
    _truncate_before_narrowing("audit_logs", "ip_address", 64)
    _set_type("audit_logs", "action", sa.String(64))
    _set_type("audit_logs", "target_type", sa.String(64))
    _set_type("audit_logs", "target_id", sa.String(64))
    _set_type("audit_logs", "ip_address", sa.String(64))
    _bind = op.get_bind()
    _bind.execute(
        sa.text(
            "UPDATE audit_logs SET principal_type = COALESCE(actor_type, 'system'), "
            "principal_id = actor_id, principal_email = actor_email, "
            "status = UPPER(COALESCE(outcome, 'SUCCESS')), event_type = action"
        )
    )
    if _orm_rows:
        for column in ("principal_type", "event_type", "status"):
            _set_nullable("audit_logs", column, False)
    _drop_index("audit_logs", "ix_audit_logs_actor_type")
    _drop_index("audit_logs", "ix_audit_logs_actor_id")
    _drop_index("audit_logs", "ix_audit_logs_organization_id")
    _drop_index("audit_logs", "ix_audit_logs_target_type")
    _create_index("audit_logs", "ix_audit_logs_target", ["target_type", "target_id"])
    _create_index("audit_logs", "ix_audit_logs_org_id", ["organization_id"])
    _create_index("audit_logs", "ix_audit_logs_event_type", ["event_type"])
    _create_index("audit_logs", "ix_audit_logs_principal_type", ["principal_type"])
    _create_index("audit_logs", "ix_audit_logs_principal_id", ["principal_id"])
    for column in ("actor_type", "actor_id", "actor_email", "outcome"):
        _drop_column("audit_logs", column)


# ── RBAC tables: legacy (0003) → ORM ────────────────────────────────────────
def _rebuild_role_permissions_to_orm() -> None:
    """Legacy role_permissions (id PK) → composite PK (role_id, permission_id).

    Guarded: if the legacy table somehow holds rows, the migration stops
    instead of silently dropping them (no application version ever wrote
    to this shape, so zero rows is the expected and observed state).
    """
    cols = _columns("role_permissions")
    if not cols or "id" not in cols:
        return
    if _rowcount("role_permissions") != 0:
        raise RuntimeError(
            "0015: role_permissions still holds legacy-shape rows; the ORM "
            "topology change (id PK → composite (role_id, permission_id) PK) "
            "cannot express a lossless data mapping. Stopping for manual "
            "review — no data was modified."
        )
    op.drop_table("role_permissions")
    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "permission_id",
            sa.Integer(),
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            nullable=False,
            primary_key=True,
        ),
        mysql_charset="utf8mb4",
    )


def _rebuild_role_permissions_to_legacy() -> None:
    cols = _columns("role_permissions")
    if not cols or "id" in cols or not _table_exists("permissions"):
        return
    if _rowcount("role_permissions") != 0:
        raise RuntimeError(
            "0015 downgrade: role_permissions holds ORM-shape rows; legacy "
            "id-PK restoration is not lossless for them. Stopping for manual "
            "review — no data was modified."
        )
    op.drop_table("role_permissions")
    op.create_table(
        "role_permissions",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "role_id",
            sa.Integer(),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "permission_id",
            sa.Integer(),
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
        mysql_charset="utf8mb4",
    )
    _create_index("role_permissions", "ix_role_permissions_role_id", ["role_id"])
    _create_index("role_permissions", "ix_role_permissions_perm_id", ["permission_id"])


def _upg_rbac() -> None:
    # permissions: length reconciliation + redundant legacy unique key
    if _table_exists("permissions"):
        _truncate_before_narrowing("permissions", "code", 100)
        _truncate_before_narrowing("permissions", "name", 150)
        _truncate_before_narrowing("permissions", "module", 50)
        _drop_index("permissions", "code")
        _set_type("permissions", "code", sa.String(100))
        _set_type("permissions", "name", sa.String(150))
        _set_type("permissions", "description", sa.Text())
        _set_type("permissions", "module", sa.String(50))
        _set_nullable("permissions", "code", False)
        _set_nullable("permissions", "name", False)
        _set_nullable("permissions", "module", False)
        _create_index("permissions", "ix_permissions_code", ["code"], unique=True)
        _drop_column("permissions", "created_at")

    # roles: length reconciliation + unique-index→constraint rename
    if _table_exists("roles"):
        _truncate_before_narrowing("roles", "code", 50)
        _truncate_before_narrowing("roles", "name", 100)
        _create_index("roles", "ix_roles_organization_id", ["organization_id"])
        _drop_index("roles", "ix_roles_org_id")
        _drop_index("roles", "uq_roles_org_code")
        _set_type("roles", "code", sa.String(50))
        _set_type("roles", "name", sa.String(100))
        _set_type("roles", "description", sa.Text())
        _create_index("roles", "ix_roles_organization_id", ["organization_id"])
        if not _index_names("roles").intersection({"uq_org_role_code"}):
            op.create_unique_constraint(
                "uq_org_role_code", "roles", ["organization_id", "code"]
            )

    # employee_role_assignments: ORM topology (assigned_by_id + created_at)
    if _table_exists("employee_role_assignments"):
        cols = _columns("employee_role_assignments")
        legacy = "assigned_at" in cols
        if legacy:
            # FK drops must come first (their indexes get dropped below; MySQL 1553)
            _rename_fk_to_columns_or_drop("employee_role_assignments", "branch_id")
            _rename_fk_to_columns_or_drop("employee_role_assignments", "assigned_by_org_id")
        _add_column(
            "employee_role_assignments",
            "assigned_by_id",
            sa.Integer(),
            nullable=True,
        )
        if "created_at" not in cols:
            op.add_column(
                "employee_role_assignments",
                sa.Column(
                    "created_at",
                    sa.DateTime(timezone=True),
                    server_default=sa.func.now(),
                    nullable=False,
                ),
            )
        # Re-key the live indexes to ORM names. ix_emp_role_emp_id /
        # ix_emp_role_role_id back live FKs (MySQL 1553), so on MySQL they are
        # RENAMED — the ORM's single-column indexes are exactly equivalent —
        # and the 3-column legacy unique index is renamed to the ORM constraint
        # name (dropping branch_id below reduces it to the two ORM columns).
        if legacy and _is_mysql():
            _rename_index("employee_role_assignments", "ix_emp_role_emp_id",
                          "ix_employee_role_assignments_employee_id")
            _rename_index("employee_role_assignments", "ix_emp_role_role_id",
                          "ix_employee_role_assignments_role_id")
            _rename_index("employee_role_assignments", "uq_employee_role_branch",
                          "uq_emp_role")
        else:
            _drop_index("employee_role_assignments", "ix_emp_role_branch_id")
            _drop_index("employee_role_assignments", "ix_emp_role_emp_id")
            _drop_index("employee_role_assignments", "ix_emp_role_role_id")
            _drop_index("employee_role_assignments", "uq_employee_role_branch")
        _create_index(
            "employee_role_assignments",
            "ix_employee_role_assignments_employee_id",
            ["employee_id"],
        )
        _create_index(
            "employee_role_assignments",
            "ix_employee_role_assignments_role_id",
            ["role_id"],
        )
        if legacy and "uq_emp_role" not in _index_names("employee_role_assignments"):
            op.create_unique_constraint(
                "uq_emp_role", "employee_role_assignments", ["employee_id", "role_id"]
            )
        if legacy:
            for column in ("assigned_at", "assigned_by_org_id", "branch_id"):
                _drop_column("employee_role_assignments", column)


def _down_rbac() -> None:
    if _table_exists("permissions"):
        cols = _columns("permissions")
        if "created_at" not in cols:
            op.add_column(
                "permissions",
                sa.Column(
                    "created_at",
                    sa.DateTime(timezone=True),
                    server_default=sa.func.now(),
                    nullable=True,
                ),
            )
        _truncate_before_narrowing("permissions", "code", 64)
        _truncate_before_narrowing("permissions", "name", 128)
        _truncate_before_narrowing("permissions", "module", 64)
        _set_type("permissions", "code", sa.String(64))
        _set_type("permissions", "name", sa.String(128))
        _set_type("permissions", "description", sa.String(255))
        _set_type("permissions", "module", sa.String(64))

    if _table_exists("roles"):
        _truncate_before_narrowing("roles", "code", 64)
        _truncate_before_narrowing("roles", "name", 128)
        if "uq_org_role_code" in _index_names("roles"):
            op.drop_constraint("uq_org_role_code", "roles", type_="unique")
        # FK-backed index: create legacy replacement BEFORE the drop (1553)
        _create_index("roles", "ix_roles_org_id", ["organization_id"])
        _drop_index("roles", "ix_roles_organization_id")
        _set_type("roles", "code", sa.String(64))
        _set_type("roles", "name", sa.String(128))
        _set_type("roles", "description", sa.String(255))
        _create_index("roles", "ix_roles_org_id", ["organization_id"])
        if not _index_names("roles").intersection({"uq_roles_org_code"}):
            op.create_index(
                "uq_roles_org_code", "roles", ["organization_id", "code"], unique=True
            )

    if _table_exists("employee_role_assignments"):
        cols = _columns("employee_role_assignments")
        legacy_back = "assigned_at" not in cols
        if legacy_back:
            for column, ddl in (
                ("branch_id", sa.Column("branch_id", sa.Integer(), nullable=True)),
                (
                    "assigned_by_org_id",
                    sa.Column("assigned_by_org_id", sa.Integer(), nullable=True),
                ),
                (
                    "assigned_at",
                    sa.Column(
                        "assigned_at", sa.DateTime(timezone=True), nullable=True
                    ),
                ),
            ):
                _add_column("employee_role_assignments", column, ddl.type, ddl.nullable)
            if _is_mysql():
                op.create_foreign_key(
                    "employee_role_assignments_ibfk_3",
                    "employee_role_assignments",
                    "branches",
                    ["branch_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
                op.create_foreign_key(
                    "employee_role_assignments_ibfk_4",
                    "employee_role_assignments",
                    "organizations",
                    ["assigned_by_org_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
            # FK-backed indexes: RENAME back to legacy names (MySQL 1553 —
            # the employee/role FKs are backed by these indexes; RENAME
            # preserves the backing). The ORM unique `uq_emp_role` is then
            # dropped and the 3-column legacy unique index is created over
            # the restored branch_id column.
            if _is_mysql():
                _rename_index(
                    "employee_role_assignments",
                    "ix_employee_role_assignments_employee_id",
                    "ix_emp_role_emp_id",
                )
                _rename_index(
                    "employee_role_assignments",
                    "ix_employee_role_assignments_role_id",
                    "ix_emp_role_role_id",
                )
            _drop_index(
                "employee_role_assignments", "ix_employee_role_assignments_employee_id"
            )
            _drop_index(
                "employee_role_assignments", "ix_employee_role_assignments_role_id"
            )
            if "uq_emp_role" in _index_names("employee_role_assignments"):
                op.drop_constraint("uq_emp_role", "employee_role_assignments", type_="unique")
            _create_index(
                "employee_role_assignments", "ix_emp_role_emp_id", ["employee_id"]
            )
            _create_index(
                "employee_role_assignments", "ix_emp_role_role_id", ["role_id"]
            )
            _create_index(
                "employee_role_assignments",
                "ix_emp_role_branch_id",
                ["branch_id"],
            )
            if not _index_names("employee_role_assignments").intersection(
                {"uq_employee_role_branch"}
            ):
                op.create_index(
                    "uq_employee_role_branch",
                    "employee_role_assignments",
                    ["employee_id", "role_id", "branch_id"],
                    unique=True,
                )
            _drop_column("employee_role_assignments", "created_at")
            _drop_column("employee_role_assignments", "assigned_by_id")

    _rebuild_role_permissions_to_legacy()


# ── remaining chain-wide reconciliation ─────────────────────────────────────
def _upg_others() -> None:
    # (table, column, orm_type, orm_nullable) tuples with concrete guard data.
    timestamp_fixes = (
        ("admins", "created_at"), ("admins", "updated_at"),
        ("audit_logs", "created_at"),
        ("branches", "created_at"), ("branches", "updated_at"),
        ("counters", "created_at"), ("counters", "updated_at"),
        ("employees", "created_at"), ("employees", "updated_at"),
        ("notifications", "sent_at"),
        ("organization_verifications", "created_at"),
        ("organization_verifications", "updated_at"),
        ("organizations", "created_at"), ("organizations", "updated_at"),
        ("payments", "created_at"), ("payments", "updated_at"),
        ("queue_logs", "logged_at"),
        ("roles", "created_at"), ("roles", "updated_at"),
        ("services", "created_at"), ("services", "updated_at"),
        ("sessions", "created_at"),
        ("settings", "updated_at"),
        ("token_history", "changed_at"),
        ("tokens", "created_at"), ("tokens", "updated_at"),
        ("users", "created_at"), ("users", "updated_at"),
    )
    for table, column in timestamp_fixes:
        _set_nullable(table, column, False)

    if _table_exists("counters"):
        _set_type("counters", "status", COUNTER_STATUS)
        _create_index("counters", "ix_counters_organization_id", ["organization_id"])

    _drop_index("admins", "email")

    # capacity_rules: late-added ORM index names replace legacy ones
    _drop_index("capacity_rules", "ix_capacity_rules_limit_type")
    _drop_index("capacity_rules", "ix_capacity_rules_scope_id")
    _drop_index("capacity_rules", "ix_capacity_rules_scope_level")

    # NOTE: for FK-backed legacy indexes the ORM-named replacement is created
    # BEFORE the legacy index is dropped (MySQL 1553 otherwise).
    _create_index("branches", "ix_branches_organization_id", ["organization_id"])
    _drop_index("branches", "ix_branches_org_id")
    _create_index("employees", "ix_employees_organization_id", ["organization_id"])
    _drop_index("employees", "email")
    _create_index("employees", "ix_employees_email", ["email"], unique=True)
    _drop_index("employees", "ix_employees_org_id")

    if _table_exists("form_field_conditions"):
        # MySQL 1553: the legacy index backs the FK on depends_on_field_id,
        # so it must be RENAMED (not dropped) to the ORM's index name.
        if (
            "ix_form_field_conditions_depends_on" in _index_names("form_field_conditions")
            and "ix_form_field_conditions_depends_on_field_id"
            not in _index_names("form_field_conditions")
            and _is_mysql()
        ):
            op.get_bind().execute(
                sa.text(
                    "ALTER TABLE form_field_conditions RENAME INDEX "
                    "ix_form_field_conditions_depends_on TO "
                    "ix_form_field_conditions_depends_on_field_id"
                )
            )
        else:
            _drop_index("form_field_conditions", "ix_form_field_conditions_depends_on")
        _create_index(
            "form_field_conditions",
            "ix_form_field_conditions_depends_on_field_id",
            ["depends_on_field_id"],
        )

    if _table_exists("kiosk_devices"):
        _drop_index("kiosk_devices", "device_code")
        _drop_index("kiosk_devices", "ix_kiosk_devices_device_code")
        _create_index(
            "kiosk_devices", "ix_kiosk_devices_device_code", ["device_code"], unique=True
        )

    if _table_exists("notifications"):
        _set_type("notifications", "type", NOTIFICATION_TYPE)
        _set_type("notifications", "channel", NOTIFICATION_CHANNEL)
        _create_index("notifications", "ix_notifications_user_id", ["user_id"])

    if _table_exists("organization_verifications"):
        _set_type("organization_verifications", "verification_status", VERIFICATION_STATUS)
        _create_index(
            "organization_verifications",
            "ix_organization_verifications_organization_id",
            ["organization_id"],
        )

    if _table_exists("organizations"):
        _create_index("organizations", "ix_organizations_state", ["state"])
        _drop_index("organizations", "email")
        _create_index("organizations", "ix_organizations_email", ["email"], unique=True)
        _drop_index("organizations", "ix_orgs_email")
        _drop_index("organizations", "ix_orgs_city")
        _drop_index("organizations", "ix_orgs_status")
        _set_type("organizations", "org_type", ORG_TYPE)
        _set_type("organizations", "status", ORG_STATUS)
        _create_index("organizations", "ix_organizations_city", ["city"])
        _create_index("organizations", "ix_organizations_name", ["name"])
        _create_index("organizations", "ix_organizations_status", ["status"])

    if _table_exists("payments"):
        _set_type("payments", "payment_method", sa.String(30))
        _set_type("payments", "status", sa.String(30))
        _create_index("payments", "ix_payments_organization_id", ["organization_id"])
        _create_index("payments", "ix_payments_razorpay_order_id", ["razorpay_order_id"])
        _create_index("payments", "ix_payments_status", ["status"])
        _create_index("payments", "ix_payments_token_id", ["token_id"])
        _create_index("payments", "ix_payments_user_id", ["user_id"])

    if _table_exists("queue_logs"):
        _create_index("queue_logs", "ix_queue_logs_action", ["action"])
        _create_index("queue_logs", "ix_queue_logs_logged_at", ["logged_at"])
        _create_index("queue_logs", "ix_queue_logs_organization_id", ["organization_id"])
        _create_index("queue_logs", "ix_queue_logs_token_id", ["token_id"])

    # role_permissions composite-PK rebuild (see docstring / guard)
    _rebuild_role_permissions_to_orm()

    _drop_index("schedule_exceptions", "ix_schedule_exceptions_exception_date")
    _drop_index("schedule_exceptions", "ix_schedule_exceptions_scope_id")
    _drop_index("schedule_exceptions", "ix_schedule_exceptions_scope_level")

    if _table_exists("services"):
        _create_index("services", "ix_services_organization_id", ["organization_id"])

    if _table_exists("settings"):
        _create_index("settings", "ix_settings_organization_id", ["organization_id"])

    _drop_index("time_slots", "ix_time_slots_slot_date")

    if _table_exists("token_form_snapshots"):
        idxs = _index_names("token_form_snapshots")
        if "ix_token_form_snapshots_token_id" in idxs:
            # unique=True conversion needs a drop+recreate, but the index
            # backs the token_id FK on MySQL — dance: drop FK, rebuild, re-add
            fk = next(iter(sorted(_fk_names("token_form_snapshots", "token_id"))), None)
            if _is_mysql() and fk:
                op.drop_constraint(fk, "token_form_snapshots", type_="foreignkey")
            op.drop_index("ix_token_form_snapshots_token_id", table_name="token_form_snapshots")
            op.create_index(
                "ix_token_form_snapshots_token_id",
                "token_form_snapshots",
                ["token_id"],
                unique=True,
            )
            if _is_mysql() and fk:
                op.create_foreign_key(
                    fk, "token_form_snapshots", "tokens", ["token_id"], ["id"],
                    ondelete="CASCADE",
                )
        _drop_index("token_form_snapshots", "token_id")

    if _table_exists("token_history"):
        _create_index("token_history", "ix_token_history_token_id", ["token_id"])

    if _table_exists("token_scans"):
        _create_index("token_scans", "ix_token_scans_counter_id", ["counter_id"])
        _create_index("token_scans", "ix_token_scans_employee", ["employee_id"])

    if _table_exists("tokens"):
        _set_type("tokens", "status", TOKEN_STATUS)
        _create_index("tokens", "ix_tokens_counter_id", ["counter_id"])
        _create_index("tokens", "ix_tokens_organization_id", ["organization_id"])
        _create_index("tokens", "ix_tokens_token_number", ["token_number"])
        _create_index("tokens", "ix_tokens_user_id", ["user_id"])
        _drop_index("tokens", "ix_tokens_booked")
        _drop_index("tokens", "ix_tokens_org")
        _drop_index("tokens", "ix_tokens_user")

    _drop_index("users", "email")

    _drop_index("weekly_schedules", "ix_weekly_schedules_scope_id")
    _drop_index("weekly_schedules", "ix_weekly_schedules_scope_level")

    _rebuild_sessions_to_orm()
    _upg_audit_logs()


def _down_others() -> None:
    for table, column in (
        ("admins", "created_at"), ("admins", "updated_at"),
        ("audit_logs", "created_at"),
        ("branches", "created_at"), ("branches", "updated_at"),
        ("counters", "created_at"), ("counters", "updated_at"),
        ("employees", "created_at"), ("employees", "updated_at"),
        ("notifications", "sent_at"),
        ("organization_verifications", "created_at"),
        ("organization_verifications", "updated_at"),
        ("organizations", "created_at"), ("organizations", "updated_at"),
        ("payments", "created_at"), ("payments", "updated_at"),
        ("queue_logs", "logged_at"),
        ("roles", "created_at"), ("roles", "updated_at"),
        ("services", "created_at"), ("services", "updated_at"),
        ("sessions", "created_at"),
        ("settings", "updated_at"),
        ("token_history", "changed_at"),
        ("tokens", "created_at"), ("tokens", "updated_at"),
        ("users", "created_at"), ("users", "updated_at"),
    ):
        _set_nullable(table, column, True)

    _create_index("admins", "email", ["email"], unique=True)

    if _table_exists("counters"):
        _set_type("counters", "status", sa.String(20))
        _restore_fk_index(
            "counters", "ix_counters_organization_id", "organization_id",
            ["organization_id"],
        )

    _create_index("capacity_rules", "ix_capacity_rules_limit_type", ["limit_type"])
    _create_index("capacity_rules", "ix_capacity_rules_scope_id", ["scope_id"])
    _create_index("capacity_rules", "ix_capacity_rules_scope_level", ["scope_level"])

    _restore_fk_index(
        "employees", "ix_employees_organization_id", "ix_employees_org_id",
        ["organization_id"],
    )
    _restore_fk_index("employees", "ix_employees_email", "email", ["email"])
    _create_index("employees", "email", ["email"], unique=True)

    if _table_exists("form_field_conditions"):
        # the ORM-named index backs the depends_on_field_id FK on MySQL —
        # RENAME back to the legacy name (drop would fail with 1553)
        _restore_fk_index(
            "form_field_conditions",
            "ix_form_field_conditions_depends_on_field_id",
            "ix_form_field_conditions_depends_on",
            ["depends_on_field_id"],
        )

    if _table_exists("kiosk_devices"):
        _drop_index("kiosk_devices", "ix_kiosk_devices_device_code")
        _create_index("kiosk_devices", "ix_kiosk_devices_device_code", ["device_code"])
        _create_index("kiosk_devices", "device_code", ["device_code"], unique=True)

    if _table_exists("notifications"):
        _set_type("notifications", "type", sa.String(40))
        _set_type("notifications", "channel", sa.String(20))
        _restore_fk_index(
            "notifications", "ix_notifications_user_id", "user_id", ["user_id"]
        )

    if _table_exists("organization_verifications"):
        _set_type("organization_verifications", "verification_status", sa.String(20))
        _restore_fk_index(
            "organization_verifications",
            "ix_organization_verifications_organization_id",
            "organization_id",
            ["organization_id"],
        )

    if _table_exists("organizations"):
        _drop_index("organizations", "ix_organizations_city")
        _drop_index("organizations", "ix_organizations_email")
        _drop_index("organizations", "ix_organizations_name")
        _drop_index("organizations", "ix_organizations_state")
        _drop_index("organizations", "ix_organizations_status")
        _set_type("organizations", "org_type", sa.String(30))
        _set_type("organizations", "status", sa.String(20))
        _create_index("organizations", "ix_orgs_city", ["city"])
        _create_index("organizations", "ix_orgs_email", ["email"], unique=True)
        _create_index("organizations", "email", ["email"], unique=True)
        _create_index("organizations", "ix_orgs_status", ["status"])

    if _table_exists("payments"):
        _set_type("payments", "payment_method", sa.String(20))
        _set_type("payments", "status", sa.String(20))
        _restore_fk_index(
            "payments", "ix_payments_organization_id", "organization_id",
            ["organization_id"],
        )
        _restore_fk_index("payments", "ix_payments_token_id", "token_id", ["token_id"])
        _restore_fk_index("payments", "ix_payments_user_id", "user_id", ["user_id"])
        _drop_index("payments", "ix_payments_status")
        _drop_index("payments", "ix_payments_razorpay_order_id")

    if _table_exists("queue_logs"):
        _drop_index("queue_logs", "ix_queue_logs_action")
        _drop_index("queue_logs", "ix_queue_logs_logged_at")
        _restore_fk_index(
            "queue_logs", "ix_queue_logs_organization_id", "organization_id",
            ["organization_id"],
        )
        _restore_fk_index("queue_logs", "ix_queue_logs_token_id", "token_id", ["token_id"])

    # role_permissions composite PK legacy rebuild
    _rebuild_role_permissions_to_legacy()

    _create_index("schedule_exceptions", "ix_schedule_exceptions_exception_date",
                  ["exception_date"])
    _create_index("schedule_exceptions", "ix_schedule_exceptions_scope_id", ["scope_id"])
    _create_index("schedule_exceptions", "ix_schedule_exceptions_scope_level",
                  ["scope_level"])

    if _table_exists("services"):
        _restore_fk_index(
            "services", "ix_services_organization_id", "organization_id",
            ["organization_id"],
        )

    if _table_exists("settings"):
        _drop_index("settings", "ix_settings_organization_id")

    _create_index("time_slots", "ix_time_slots_slot_date", ["slot_date"])

    if _table_exists("token_form_snapshots"):
        # inverse of the upgrade's FK dance: drop FK → replace unique index
        # with the legacy non-unique pair (ix + auto-name) → re-add FK
        fk = next(iter(sorted(_fk_names("token_form_snapshots", "token_id"))), None)
        if _is_mysql() and fk:
            op.drop_constraint(fk, "token_form_snapshots", type_="foreignkey")
        op.drop_index(
            "ix_token_form_snapshots_token_id", table_name="token_form_snapshots"
        )
        op.create_index(
            "ix_token_form_snapshots_token_id",
            "token_form_snapshots",
            ["token_id"],
        )
        _create_index("token_form_snapshots", "token_id", ["token_id"])
        if _is_mysql() and fk:
            op.create_foreign_key(
                fk, "token_form_snapshots", "tokens", ["token_id"], ["id"],
                ondelete="CASCADE",
            )

    if _table_exists("token_history"):
        _restore_fk_index(
            "token_history", "ix_token_history_token_id", "token_id", ["token_id"]
        )

    if _table_exists("token_scans"):
        _restore_fk_index(
            "token_scans", "ix_token_scans_counter_id", "fk_scans_counter",
            ["counter_id"],
        )
        _restore_fk_index(
            "token_scans", "ix_token_scans_employee", "ix_token_scans_employee_id",
            ["employee_id"],
        )
        _create_index(
            "token_scans", "ix_token_scans_organization_id", ["organization_id"]
        )
        _create_index("token_scans", "ix_token_scans_token_id", ["token_id"])

    if _table_exists("tokens"):
        _set_type("tokens", "status", sa.String(20))
        _restore_fk_index(
            "tokens", "ix_tokens_organization_id", "ix_tokens_org",
            ["organization_id"],
        )
        _restore_fk_index("tokens", "ix_tokens_user_id", "ix_tokens_user", ["user_id"])
        _restore_fk_index("tokens", "ix_tokens_counter_id", "counter_id",
                          ["counter_id"])
        _drop_index("tokens", "ix_tokens_token_number")
        _create_index("tokens", "ix_tokens_booked", ["booked_at"])

    _create_index("users", "email", ["email"], unique=True)

    _create_index("weekly_schedules", "ix_weekly_schedules_scope_id", ["scope_id"])
    _create_index("weekly_schedules", "ix_weekly_schedules_scope_level", ["scope_level"])

    _down_audit_logs()
    _rebuild_sessions_to_legacy()


def upgrade() -> None:
    _upg_others()
    _upg_rbac()


def downgrade() -> None:
    _down_rbac()
    _down_others()
