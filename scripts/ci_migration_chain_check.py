"""
scripts/ci_migration_chain_check.py
───────────────────────────────────
CI migration verification for SkipLine (Phase-9 production readiness).

Runs the REAL alembic chain against a DISPOSABLE MySQL 8 database created by
the script itself, then:

    upgrade 0001..0017  ->  seed legacy rows  ->  upgrade head (0018)
    -> FK audit (information_schema)  -> named-index audit
    -> downgrade 0017 (MySQL error-1553 regression check)
    -> re-upgrade head (idempotence)
    -> drop the disposable database (only if this run created it)

Environment (all synthetic, CI-only — never production values):
    DB_HOST      MySQL host — MUST be loopback, i.e. 127.0.0.1 or localhost
                 (default 127.0.0.1). The script never connects to a remote
                 host, so it can never reach a remote production database.
    DB_PORT      MySQL port 1..65535                        (default 3306)
    DB_USER      MySQL user with create/drop rights        (default root)
    DB_PASSWORD  password for DB_USER                      (required, synthetic)
    DB_NAME      disposable database name — EXACTLY
                 "skipline_ci_migrate" (allowlist, see below)

Safety rules enforced here (pre-activation security review, Nov 6):
    - DB_NAME is matched against an EXACT allowlist, not a prefix: the real
      project database ('skipline'), production-like names, empty values and
      every other name are rejected before any connection is opened;
    - DB_HOST must resolve to loopback — destructive SQL is only ever sent
      to a host the operator explicitly pointed at a disposable container;
    - DB_PORT is validated as an integer 1..65535; DB_USER/DB_PASSWORD must
      be non-empty synthetic values;
    - PRE-EXISTING DATABASES ARE PRESERVED: if 'skipline_ci_migrate' already
      exists on the target server before this run starts, the script refuses
      to proceed and never executes a single DROP — a database this run did
      not create is never dropped. (There is no unconditional 'DROP IF
      EXISTS' before creation anywhere in this script.)
    - EVERY DROP statement passes drop_disposable_check(target) (exact
      allowlist) AND the ownership flag (created_this_run): teardown runs
      only after this execution verifiably created the database, only for
      that exact database, and only through perform_drop's redaction-wrapped
      engine — so no exception path can ever drop anything else;
    - credentials are never printed (URL strings are redacted in errors);
    - no payment, mail or AI configuration is touched.

The module has NO import-time side effects (pure function definitions), so
hermetic gate tests (tests/test_ci_migration_gate.py) can import it freely;
execution happens via python scripts/ci_migration_chain_check.py.

Prints 'ALL-OK: ...' on success; any failure exits non-zero with a clear,
redacted message so the workflow step fails with the exact cause.
"""
import os
import sys
import time
from typing import NoReturn

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# ── Exact allowlist — the ONLY database this script is ever allowed to
#    create or drop. (Prefix matching was judged insufficient in the
#    pre-activation security review: e.g. 'skipline' itself would have
#    passed a 'skipline_ci*' check had SQL been quoted defensively there,
#    and 'skipline_ci_prodcheck' style ad-hoc names create unbounded
#    destructive surface.) ────────────────────────────────────────────────
ALLOWED_MIGRATION_DB = "skipline_ci_migrate"
ALLOWED_MIGRATION_HOSTS = ("127.0.0.1", "localhost")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PHASE9 = {
    "service_workflows", "workflow_stages", "token_workflow_states",
    "token_stage_logs", "ai_conversations",
}

DECLARED_INDEXES = {
    ("service_workflows", "ix_service_workflows_organization_id"),
    ("workflow_stages", "ix_workflow_stages_workflow_id"),
    ("workflow_stages", "ix_workflow_stages_organization_id"),
    ("token_workflow_states", "ix_token_workflow_states_token_id"),
    ("token_workflow_states", "ix_token_workflow_states_organization_id"),
    ("token_stage_logs", "ix_token_stage_logs_token_id"),
    ("token_stage_logs", "ix_token_stage_logs_organization_id"),
    ("token_stage_logs", "ix_token_stage_logs_action"),
    ("ai_conversations", "ix_ai_conversations_user_id"),
}

# The EXACT foreign keys migrations/versions/0018_workflows_and_ai.py
# declares on the phase-9 tables, as (TABLE_NAME, CONSTRAINT_NAME,
# REFERENCED_TABLE_NAME, DELETE_RULE). 0018 declares 14 FKs whose
# (table, delete_rule) pairs collapse to 9 distinct combinations — so the
# former fuzzy '>= 10 distinct-pairs' assertion was unachievable by
# construction and could not distinguish a missing FK from a collapsed
# duplicate. The audit is now an exact equality against this declared set:
# every missing, dropped or unexpected FK is named, which is strictly
# stronger than the old count-based check.
DECLARED_FKS = {
    ("service_workflows", "fk_service_workflow_org", "organizations",
     "CASCADE"),
    ("service_workflows", "fk_service_workflow_service", "services",
     "CASCADE"),
    ("workflow_stages", "fk_workflow_stage_workflow", "service_workflows",
     "CASCADE"),
    ("workflow_stages", "fk_workflow_stage_org", "organizations", "CASCADE"),
    ("workflow_stages", "fk_workflow_stage_counter", "counters", "SET NULL"),
    ("token_workflow_states", "fk_token_workflow_state_token", "tokens",
     "CASCADE"),
    ("token_workflow_states", "fk_token_workflow_state_org", "organizations",
     "CASCADE"),
    ("token_workflow_states", "fk_token_workflow_state_workflow",
     "service_workflows", "RESTRICT"),
    ("token_workflow_states", "fk_token_workflow_state_stage",
     "workflow_stages", "SET NULL"),
    ("token_stage_logs", "fk_token_stage_log_token", "tokens", "CASCADE"),
    ("token_stage_logs", "fk_token_stage_log_org", "organizations",
     "CASCADE"),
    ("token_stage_logs", "fk_token_stage_log_state",
     "token_workflow_states", "CASCADE"),
    ("token_stage_logs", "fk_token_stage_log_employee", "employees",
     "SET NULL"),
    ("ai_conversations", "fk_ai_conversation_user", "users", "CASCADE"),
}

# Synthetic (non-secret) seed values — schema-aware NOT NULL fill-in below.
LEGACY_ROWS = [
    ("organizations", {
        "id": 1, "name": "CI Check Org", "email": "ci-check@example.test",
        "status": "approved",
    }),
    ("services", {"id": 1, "name": "CI Check Service"}),
    ("counters", {"id": 1, "counter_name": "CI Check Counter"}),
    ("tokens", {
        "id": 1, "organization_id": 1, "counter_id": 1, "service_id": 1,
        "status": "waiting", "token_number": "CI-0001", "queue_position": 1,
    }),
    ("tokens", {
        "id": 2, "organization_id": 1, "counter_id": 1, "service_id": 1,
        "status": "waiting", "token_number": "CI-0002", "queue_position": 2,
    }),
]


def validate_ci_config(env: dict) -> dict:
    """Validate and normalize the CI configuration. Raises SystemExit with a
    refusal message for every rejected configuration; never connects.

    Hermetic gate tests exercise allowed and rejected names through this
    exact function (tests/test_ci_migration_gate.py)."""
    def refuse(reason: str) -> NoReturn:
        sys.exit(f"REFUSING TO RUN: {reason}")

    db_name = (env.get("DB_NAME") or "").strip()
    db_host = (env.get("DB_HOST") or "127.0.0.1").strip()
    db_port_raw = (env.get("DB_PORT") or "3306").strip()
    db_user = (env.get("DB_USER") or "").strip()
    db_password = env.get("DB_PASSWORD") or ""

    # Database name: EXACT allowlist. Empty, production-like
    # ('skipline', 'skipline_migrate', …) and every unexpected name is
    # rejected before any connection is opened.
    if not db_name:
        refuse("DB_NAME is empty — a disposable database name is required.")
    if db_name != ALLOWED_MIGRATION_DB:
        refuse(
            f"DB_NAME='{db_name}' is not permitted. The CI migration check "
            f"may only target the exact disposable database "
            f"'{ALLOWED_MIGRATION_DB}'. The real project database 'skipline' "
            "and every other name are rejected."
        )

    # Connection host: loopback only — the script must never be pointed at
    # a remote (potentially shared/production) MySQL server.
    if db_host not in ALLOWED_MIGRATION_HOSTS:
        refuse(
            f"DB_HOST='{db_host}' is not permitted — only loopback hosts "
            f"{list(ALLOWED_MIGRATION_HOSTS)} are allowed so destructive SQL "
            "can never reach a remote server."
        )

    # Port: strict integer 1..65535.
    try:
        db_port = int(db_port_raw)
    except ValueError:
        refuse(f"DB_PORT='{db_port_raw}' is not an integer.")
    if not (1 <= db_port <= 65535):
        refuse(f"DB_PORT={db_port} is outside 1..65535.")

    # User/password: non-empty (synthetic CI value, never production).
    if not db_user:
        refuse("DB_USER is empty — the CI service user is required "
               "(synthetic value; production credentials are forbidden).")
    if not db_password:
        refuse("DB_PASSWORD is empty — the CI service password is required "
               "(synthetic value; production credentials are forbidden).")
    if not db_password.strip():
        refuse("DB_PASSWORD is whitespace-only — a usable (synthetic) "
               "CI service password is required.")

    return {
        "db_name": db_name,
        "db_host": db_host,
        "db_port": db_port,
        "db_user": db_user,
        "db_password": db_password,
    }


def drop_disposable_check(db_name: str) -> None:
    """Hard guard before ANY destructive statement: only the exact
    allowlisted database may ever be dropped — regardless of any exception
    path that might lead here. Exits the process on any non-allowlisted
    name; returns normally when the guard passes."""
    if db_name != ALLOWED_MIGRATION_DB:
        sys.exit(
            f"SAFETY GATE TRIPPED: refusal to drop database '{db_name}' — "
            f"only '{ALLOWED_MIGRATION_DB}' may be dropped by this script."
        )


def refuse_if_preexisting(existing_names, db_name: str) -> None:
    """Pre-creation guard: refuse to run (system exit) when the allowlisted
    target database ALREADY EXISTS on the server. A database this execution
    did not create is never touched, let alone dropped — the operator (or a
    fresh CI container) must remove leftovers. Hermetic tests prove a
    pre-existing database is preserved end-to-end through this function."""
    if db_name in set(existing_names):
        sys.exit(
            f"REFUSING TO RUN: database '{db_name}' already exists on the "
            "target server and was NOT created by this execution. This "
            "script never drops a database it did not create in the "
            "current run; remove the leftover manually on the disposable "
            "server (or use a fresh CI container) and rerun."
        )


def teardown_allowed(target: str, created_this_run: bool) -> bool:
    """Pure teardown policy: an execution may drop `target` ONLY if
    (a) it is the exact allowlist name and (b) THIS run created it. Used
    before every teardown decision so no failure path can drop a database
    this run does not verifiably own."""
    drop_disposable_check(target)
    return bool(created_this_run)


def perform_drop(target: str, engine_factory, redactor) -> None:
    """Execute the guarded DROP against a disposable server. The allowlist
    guard runs BEFORE the engine is even created; `engine_factory` is
    injected so hermetic tests can record every executed statement without
    touching any database."""
    drop_disposable_check(target)
    engine = engine_factory()
    try:
        with engine.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {target}"))
            c.commit()
        print("[7] disposable database dropped")
    except Exception as e:  # noqa: BLE001
        print(f"[7] WARNING: disposable database drop failed "
              f"(non-fatal): {redactor(str(e))}")
    finally:
        engine.dispose()


def redact(message: str, db_url_str: str, password: str) -> str:
    """Remove the password and the connection URL from any message before it
    reaches CI logs. Order matters: the URL (which CONTAINS the password) is
    replaced first, then any bare password occurrence. Empty inputs are
    skipped (replacing "" would corrupt the whole message)."""
    if db_url_str:
        message = message.replace(db_url_str, "<disposable-db-url>")
    if password:
        message = message.replace(password, "***")
    return message


def step_failed(step: str, exc: Exception, redactor) -> NoReturn:
    raise RuntimeError(f"{step} FAILED: {redactor(str(exc))}") from exc


def main(env: dict = None) -> int:
    env = os.environ if env is None else env
    cfg_env = validate_ci_config(env)
    db_name = cfg_env["db_name"]
    db_password = cfg_env["db_password"]

    os.chdir(PROJECT_ROOT)

    db_url = make_url(
        f"mysql+pymysql://{cfg_env['db_user']}:{db_password}"
        f"@{cfg_env['db_host']}:{cfg_env['db_port']}/{db_name}"
    )
    server_url = db_url.set(database="")
    db_url_str = db_url.render_as_string(hide_password=False)

    def rd(message: str) -> str:
        return (message.replace(db_password or "*", "***")
                .replace(db_url_str, "<disposable-db-url>")
                .replace(str(db_url), "<disposable-db-url>"))

    def drop_disposable(target: str, created_this_run: bool) -> None:
        if not teardown_allowed(target, created_this_run):
            print(f"[7] -> {target} exists but was NOT created by this "
                  "execution; preserved, never dropped.")
            return
        perform_drop(
            target,
            lambda: create_engine(
                server_url.render_as_string(hide_password=False),
                pool_pre_ping=True),
            rd)

    server = create_engine(server_url.render_as_string(hide_password=False),
                           pool_pre_ping=True)
    created_this_run = False
    try:
        with server.connect() as c:
            existing = [r[0] for r in
                        c.execute(text("SHOW DATABASES")).fetchall()]
            # pre-creation guard: a database this run did not create is
            # never dropped — refuse and leave it entirely untouched
            refuse_if_preexisting(existing, db_name)
            c.execute(text(
                f"CREATE DATABASE {db_name} "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
            c.commit()
        created_this_run = True
        print(f"[1] disposable database {db_name} created")
    except Exception as e:  # noqa: BLE001  (SystemExit from the guard passes through)
        server.dispose()
        sys.exit(f"database setup FAILED: {rd(str(e))}")

    cfg = Config("alembic.ini")  # script_location=migrations handled by env.py
    engine = create_engine(db_url_str, pool_pre_ping=True)

    def insert_row(table, explicit):
        with engine.connect() as c:
            cols = c.execute(text(
                "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
                "COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t "
                "  AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL "
                "  AND EXTRA NOT LIKE '%auto_increment%'"
            ), {"db": db_name, "t": table}).fetchall()

        values = dict(explicit)
        for name, dtype, maxlen, coltype in cols:
            if name in values:
                continue
            if dtype == "enum":
                values[name] = (coltype.split("(", 1)[1].rsplit(")", 1)[0]
                                .split(",", 1)[0].strip().strip("'"))
            elif dtype == "set":
                values[name] = ""
            elif dtype.startswith(("char", "varchar")) or dtype == "text":
                values[name] = ("CI" + name)[: min(maxlen or 64, 64) - 1]
            elif dtype in ("int", "bigint", "smallint", "tinyint"):
                values[name] = 1
            elif dtype in ("decimal", "double", "float"):
                values[name] = 0
            elif dtype in ("datetime", "timestamp"):
                values[name] = "NOW()"
            else:
                values[name] = "CICheck"

        names = list(values.keys())
        set_sql = ", ".join(
            f"`{n}` = NOW()" if values[n] == "NOW()" else f"`{n}` = :v_{n}"
            for n in names)
        params = {f"v_{n}": values[n] for n in names if values[n] != "NOW()"}
        with engine.begin() as c:
            c.execute(text(f"INSERT INTO `{table}` SET {set_sql}"), params)

    def audit():
        with engine.connect() as c:
            tables = {r[0] for r in c.execute(text("SHOW TABLES")).fetchall()}
            hit = sorted(PHASE9 & tables)
            legacy = c.execute(text(
                "SELECT COUNT(*) FROM tokens WHERE id IN (1, 2)")).scalar()
            org = c.execute(text(
                "SELECT COUNT(*) FROM organizations WHERE id = 1")).scalar()
            svc = c.execute(text(
                "SELECT COUNT(*) FROM services WHERE id = 1")).scalar()
            ver = c.execute(
                text("SELECT version_num FROM alembic_version")).scalar()
            fks = c.execute(text(
                "SELECT rc.TABLE_NAME, rc.CONSTRAINT_NAME, "
                "rc.REFERENCED_TABLE_NAME, rc.DELETE_RULE "
                "FROM information_schema.REFERENTIAL_CONSTRAINTS rc "
                "JOIN information_schema.KEY_COLUMN_USAGE k "
                "  ON rc.CONSTRAINT_NAME = k.CONSTRAINT_NAME "
                " AND rc.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA "
                "WHERE rc.CONSTRAINT_SCHEMA = :db "
                "  AND rc.TABLE_NAME IN "
                "('service_workflows','workflow_stages',"
                "'token_workflow_states','token_stage_logs','ai_conversations')"
            ), {"db": db_name}).fetchall()
            idx = c.execute(text(
                "SELECT DISTINCT TABLE_NAME, INDEX_NAME "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME IN "
                "('service_workflows','workflow_stages',"
                "'token_workflow_states','token_stage_logs','ai_conversations')"
            ), {"db": db_name}).fetchall()
            return (ver, hit, (legacy, org, svc),
                    {(r[0], r[1], r[2], r[3]) for r in fks},
                    {(r[0], r[1]) for r in idx})

    def check(step):
        ver, hit, legacy, fks, idx = audit()
        print(f"    [{step}] alembic_version={ver}; phase-9 tables={hit}; "
              f"legacy rows (tokens/org/service)={legacy}; "
              f"FK constraints on 0018 tables={len(fks)}/{len(DECLARED_FKS)}; "
              f"named indexes on 0018 tables="
              f"{len(idx & DECLARED_INDEXES)}/{len(DECLARED_INDEXES)}")
        return ver, hit, legacy, fks, idx

    success = False
    try:
        try:
            t0 = time.time()
            command.upgrade(cfg, "0017")
            print(f"[2] full chain 0001→0017 OK in {time.time() - t0:.1f}s")

            for table, explicit in LEGACY_ROWS:
                insert_row(table, explicit)
            print("[3] legacy rows seeded (2 tokens, org, service, counter)")

            t0 = time.time()
            command.upgrade(cfg, "head")  # -> 0018
            ver, hit, legacy, fks, idx = check("after-up")
            assert ver == "0018", f"expected version 0018, got {ver}"
            assert set(hit) == PHASE9, \
                f"0018 tables incomplete: missing {PHASE9 - set(hit)}"
            assert legacy == (2, 1, 1), \
                f"legacy rows lost by upgrade: {legacy}"
            missing_idx = DECLARED_INDEXES - idx
            assert not missing_idx, \
                f"declared indexes missing after upgrade: {missing_idx}"
            missing_fks = DECLARED_FKS - fks
            unexpected_fks = fks - DECLARED_FKS
            assert not missing_fks, \
                f"declared FKs missing after upgrade: {sorted(missing_fks)}"
            assert not unexpected_fks, \
                f"unexpected FKs on phase-9 tables: {sorted(unexpected_fks)}"
            print(f"[4] upgrade head (0018) OK in {time.time() - t0:.1f}s; "
                  f"FK set={sorted(fks)}")

            t0 = time.time()
            command.downgrade(cfg, "0017")
            ver, hit, legacy, _, _ = check("after-down")
            assert ver == "0017", f"expected version 0017, got {ver}"
            assert not hit, f"phase-9 tables not fully removed: {hit}"
            assert legacy == (2, 1, 1), \
                f"legacy rows lost by downgrade: {legacy}"
            print(f"[5] downgrade 0018→0017 OK (no MySQL error 1553) in "
                  f"{time.time() - t0:.1f}s")

            t0 = time.time()
            command.upgrade(cfg, "head")
            ver, hit, legacy, fks2, idx2 = check("after-reup")
            assert ver == "0018" and set(hit) == PHASE9
            assert legacy == (2, 1, 1)
            assert fks2 == fks, \
                "FK set differs after re-upgrade (idempotence broken)"
            assert not (DECLARED_INDEXES - idx2), \
                "named indexes missing after re-upgrade"
            print(f"[6] re-upgrade (idempotence) OK in "
                  f"{time.time() - t0:.1f}s")
            success = True
        except AssertionError:
            raise
        except Exception as e:  # noqa: BLE001
            step_failed("migration chain", e, rd)
    finally:
        # guarantee teardown even on failure — but ONLY for the database this
        # run created. Every destructor call passes the ownership + allowlist
        # gates first.
        engine.dispose()
        server.dispose()
        drop_disposable(db_name, created_this_run)

    if not success:
        sys.exit("MIGRATION CHAIN FAILED (see messages above; the disposable "
                 "database created by this run was dropped).")

    print("ALL-OK: real alembic chain 0001→0018 up/down/re-up verified;"
          " all 14 declared FKs and 9/9 declared indexes match the migration;"
          " legacy rows preserved at every step; no error 1553.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
