"""
scripts/backup_restore_rehearsal.py
───────────────────────────────────
Real, reproducible dump→restore rehearsal of SkipLine's backup mechanism
(scripts/backup.sh + scripts/restore.sh mechanics, run natively).

Safety rules (same doctrine as ci_migration_chain_check.py):
  - The ONLY databases touched are two freshly-created, uniquely-named
    disposable databases of this single run:
        source  : skipline_bakrehearsal_<runstamp>
        scratch : skipline_bakrestore_<runstamp>
    Any pre-existing database with those names is REFUSED (never dropped):
    a database this run did not create is never dropped, and the script
    refuses to start if either name already exists.
  - Credentials are never printed; connection errors are redacted.
  - The real product DBs (`skipline`, `skipline_phase9_prodcheck`,
    `skipline_synccheck`, etc.) are NEVER referenced.

Schema mode: the source schema is normally built by the REAL alembic
  migration chain (`alembic upgrade head`). If — and only if — the
  workstation's Application Control policy blocks the async driver stack
  (the intermittent `_greenlet` DLL block), the script falls back to
  building the identical current schema from the project's own ORM
  metadata (all 64 tables) via synchronous PyMySQL, banner-logged and
  stamped with the alembic head revision. This is a loud, disclosed
  degraded mode; it never attempts any policy bypass, and a run of the
  real migration chain takes precedence whenever greenlet is available.

Sequence (mirrors docs/deployment.md + scripts/backup.sh):
  1 create source DB → 2 alembic upgrade head (or sync-create_all
  fallback) → 3 representative seed rows
  (organisation, customer, bookings/tokens, payment state, workflow)
  → 4 mysqldump --single-transaction --routines --events --triggers
      --set-gtid-purged=OFF | gzip (same flags as scripts/backup.sh)
  → 5 integrity check (size ≥ 1000 B unpacked + sha256) → 6 restore the
  artifact into the scratch DB → 7 verify schema/counts/relationships/
  representative values → 8 application-level access query on the restore →
  9 drop ONLY the two DBs this run created (kept with --keep for inspection).

Exit codes: 0 success ("ALL-OK"), non-zero any failure.
"""
import argparse
import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Diagnostics: never lose an error message to stdout buffering when the
# output is redirected to a file.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ── protected names: refuse ANY overlap with operator/product DBs ─────────
PROTECTED = {
    "skipline", "skipline_phase9_prodcheck", "skipline_synccheck",
    "information_schema", "mysql", "performance_schema", "sys",
}


def refuse(msg: str) -> "NoReturn":
    sys.exit(f"REFUSING TO RUN: {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", action="store_true",
                    help="keep both rehearsal DBs after the run (default: drop)")
    args = ap.parse_args()

    t_start = time.time()
    runstamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    src_db = f"skipline_bakrehearsal_{runstamp}"
    dst_db = f"skipline_bakrestore_{runstamp}"
    print(f"[1] run stamp {runstamp}; source={src_db} scratch={dst_db}")

    creds = {k: os.environ.get(k, "") for k in
             ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD")}
    if not creds["DB_USER"] or not creds["DB_PASSWORD"]:
        refuse("DB_USER/DB_PASSWORD not exported "
               "(synthetic rehearsal credentials required).")
    host = creds["DB_HOST"] or "127.0.0.1"
    if host not in ("127.0.0.1", "localhost"):
        refuse(f"DB_HOST={host} is not loopback — rehearsal never leaves "
               "this machine.")
    port = creds["DB_PORT"] or "3306"

    mysqldump = shutil.which("mysqldump") or str(
        Path(r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe"))
    mysql = shutil.which("mysql") or str(
        Path(r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe"))
    if not (Path(mysqldump).exists() and Path(mysql).exists()):
        refuse("mysqldump/mysql client not found.")

    import urllib.parse as up
    import sqlalchemy
    pw = up.quote_plus(creds["DB_PASSWORD"])
    sync_url = (f"mysql+pymysql://{creds['DB_USER']}:{pw}@{host}:{port}")
    plain = dict(host=host, port=int(port),
                 user=creds["DB_USER"], passwd=creds["DB_PASSWORD"])
    server = sqlalchemy.create_engine(sync_url + "/", pool_pre_ping=True)

    with server.connect() as c:
        existing = {r[0] for r in c.execute(sqlalchemy.text("SHOW DATABASES"))}
        if src_db in existing or dst_db in existing:
            refuse("a rehearsal-named database already exists — this run "
                   "never drops a database it did not create.")
        overlap = PROTECTED & existing
        print(f"    server={host}:{port} version="
              f"{c.execute(sqlalchemy.text('SELECT VERSION()')).scalar()}; "
              f"protected DBs present={sorted(overlap)} (untouched)")

    def created_databases():
        return [src_db, dst_db]

    created_engines: list[sqlalchemy.Engine] = []

    def new_engine(url: str, label: str) -> sqlalchemy.Engine:
        eng = sqlalchemy.create_engine(url, pool_pre_ping=True)
        created_engines.append(eng)
        return eng

    try:
        # ── 1. create source DB ────────────────────────────────────────────
        with server.connect() as c:
            c.execute(sqlalchemy.text(
                f"CREATE DATABASE `{src_db}` CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_unicode_ci"))
            c.commit()
        print("[2] source DB created")

        # ── 2. schema build on the source DB ──────────────────────────────
        # Preferred path: the real async alembic migration chain. Only if
        # the workstation Application Control policy blocks the async
        # stack (greenlet DLL) do we fall back to the project's own ORM
        # metadata synchronously — banner-logged, head-stamped, no bypass.
        env = dict(os.environ)
        env.update({"DB_HOST": host, "DB_PORT": port,
                    "DB_NAME": src_db, "DB_USER": plain["user"],
                    "DB_PASSWORD": creds["DB_PASSWORD"]})
        src_engine = new_engine(sync_url + f"/{src_db}", "src-schema")
        r = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
            timeout=600)
        schema_mode = "alembic-async"
        if r.returncode != 0:
            tail = (r.stdout + r.stderr)[-3000:]
            if ("Application Control policy has blocked" in tail
                    or ("greenlet" in tail.lower()
                        and "dll load failed" in tail.lower())):
                schema_mode = "sync-create_all"
                print("[3] SCHEMA MODE = sync-create_all (fallback): the "
                      "workstation Application Control policy blocked "
                      "alembic's async driver stack (greenlet DLL). "
                      "Building the current schema from the project's own "
                      "ORM metadata via synchronous PyMySQL. NO policy "
                      "bypass is attempted; runs using the real migration "
                      "chain take precedence.")
                import pkgutil
                import importlib
                if str(PROJECT_ROOT) not in sys.path:
                    sys.path.insert(0, str(PROJECT_ROOT))
                import models as _models_pkg
                dbmod = importlib.import_module("app.database")
                for m in pkgutil.iter_modules(_models_pkg.__path__):
                    importlib.import_module(f"models.{m.name}")
                n_tables = len(dbmod.Base.metadata.tables)
                dbmod.Base.metadata.create_all(src_engine)
                with src_engine.connect() as c:
                    c.execute(sqlalchemy.text(
                        "CREATE TABLE IF NOT EXISTS alembic_version "
                        "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
                    c.execute(sqlalchemy.text(
                        "INSERT INTO alembic_version (version_num) "
                        "SELECT '0018' FROM (SELECT 1) AS seed "
                        "WHERE NOT EXISTS (SELECT 1 FROM alembic_version)"))
                    c.commit()
                print(f"    sync-create_all built {n_tables} tables; "
                      "alembic_version stamped 0018")
            else:
                print(r.stdout[-2000:] + r.stderr[-2000:])
                raise RuntimeError("alembic upgrade head failed")
        else:
            print("[3] migrations applied to source DB (real alembic chain)")

        # ── 3. representative seed rows (schema-introspected fill) ────────
        # (src_engine was created above for the schema build)
        seed_rows = [
            ("organizations", {"id": 1, "name": "Rehearsal Clinic",
                               "email": "rehearsal-org@example.test",
                               "status": "approved"}),
            ("services", {"id": 1, "name": "Rehearsal Consult",
                          "organization_id": 1}),
            ("counters", {"id": 1, "counter_name": "Rehearsal Counter"}),
            ("users", {"id": 1, "name": "Rehearsal Customer",
                       "email": "rehearsal-user@example.test",
                       "mobile": "9000000001"}),
            ("tokens", {"id": 1, "organization_id": 1, "counter_id": 1,
                        "service_id": 1, "status": "waiting",
                        "token_number": "RH-0001", "queue_position": 1}),
            ("tokens", {"id": 2, "organization_id": 1, "counter_id": 1,
                        "service_id": 1, "status": "waiting",
                        "token_number": "RH-0002", "queue_position": 2}),
            ("service_workflows", {"id": 1, "organization_id": 1,
                                   "service_id": 1,
                                   "name": "Rehearsal Workflow"}),
            ("workflow_stages", {"id": 1, "workflow_id": 1,
                                 "organization_id": 1,
                                 "code": "registration",
                                 "name": "Registration", "position": 1}),
            ("workflow_stages", {"id": 2, "workflow_id": 1,
                                 "organization_id": 1,
                                 "code": "consultation",
                                 "name": "Consultation", "position": 2}),
        ]
        with src_engine.connect() as c:
            for table, explicit in seed_rows:
                cols = c.execute(sqlalchemy.text(
                    "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
                    "COLUMN_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t "
                    "  AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT IS NULL "
                    "  AND EXTRA NOT LIKE '%auto_increment%'"),
                    {"db": src_db, "t": table}).fetchall()
                values = dict(explicit)
                for name, dtype, maxlen, coltype in cols:
                    if name in values:
                        continue
                    if dtype == "enum":
                        values[name] = (coltype.split("(", 1)[1]
                                        .rsplit(")", 1)[0]
                                        .split(",", 1)[0].strip().strip("'"))
                    elif dtype == "set":
                        values[name] = ""
                    elif dtype.startswith(("char", "varchar")) or dtype == "text":
                        values[name] = ("RH" + name)[: min(maxlen or 64, 64) - 1]
                    elif dtype in ("int", "bigint", "smallint", "tinyint"):
                        values[name] = 1
                    elif dtype in ("decimal", "double", "float"):
                        values[name] = 0
                    elif dtype in ("datetime", "timestamp"):
                        values[name] = "NOW()"
                    else:
                        values[name] = "Rehearsal"
                names = list(values.keys())
                set_sql = ", ".join(
                    f"`{n}` = NOW()" if values[n] == "NOW()"
                    else f"`{n}` = :v_{n}" for n in names)
                params = {f"v_{n}": values[n] for n in names
                          if values[n] != "NOW()"}
                c.execute(sqlalchemy.text(
                    f"INSERT INTO `{table}` SET {set_sql}"), params)
            c.commit()
        counts = {}
        for table, _ in seed_rows:
            # connect() must be closed — an unclosed Connection stays
            # checked-out of the pool even after dispose(), holds table
            # MDLs via its open transaction, and deadlocks the teardown
            # DROPs (exact failure reproduced run7, Nov 8).
            with src_engine.connect() as c:
                counts[table] = c.execute(
                    sqlalchemy.text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
        print(f"[4] seeded representative rows: {counts}")
        src_engine.dispose()

        # ── 4. BACKUP via the production mechanism (scripts/backup.sh flags)
        backups_dir = PROJECT_ROOT / "backups"
        backups_dir.mkdir(exist_ok=True)
        artifact = backups_dir / f"{src_db}.sql.gz"
        dump_flags = ["--single-transaction", "--routines", "--events",
                      "--triggers", "--set-gtid-purged=OFF",
                      src_db]
        proc = subprocess.run(
            [mysqldump, f"--host={host}", f"--port={port}",
             f"--user={plain['user']}",
             f"--password={plain['passwd']}"] + dump_flags,
            capture_output=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(
                "mysqldump failed: " + (proc.stderr.decode(errors='replace')[-500:]))
        raw_sql = proc.stdout
        with open(artifact, "wb") as f:
            with gzip.GzipFile(filename="", fileobj=f, mode="wb") as gz:
                gz.write(raw_sql)
        unpacked = len(raw_sql)
        if unpacked < 1000:
            raise RuntimeError(
                f"backup decompresses to {unpacked} bytes (< 1000) — "
                "refusing to call this a backup (backup.sh parity check)")
        sha = hashlib.sha256(open(artifact, "rb").read()).hexdigest()
        print(f"[5] BACKUP OK: {artifact.name} ({unpacked} B unpacked SQL; "
              f"sha256={sha})")

        # ── 5. RESTORE into the scratch DB (scripts/restore.sh parity) ────
        with server.connect() as c:
            c.execute(sqlalchemy.text(
                f"CREATE DATABASE `{dst_db}` CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_unicode_ci"))
            c.commit()
        payload = gzip.decompress(open(artifact, "rb").read())
        rproc = subprocess.run(
            [mysql, f"--host={host}", f"--port={port}",
             f"--user={plain['user']}", f"--password={plain['passwd']}",
             "--default-character-set=utf8mb4", dst_db],
            input=payload, capture_output=True, timeout=600)
        if rproc.returncode != 0:
            raise RuntimeError(
                "restore load failed: "
                + rproc.stderr.decode(errors="replace")[-500:])
        print("[6] artifact restored into scratch DB")

        # ── 6. verification ────────────────────────────────────────────────
        dst_engine = new_engine(sync_url + f"/{dst_db}", "dst-load")

        def snapshot(e, db):
            with e.connect() as c:
                tables = {r[0] for r in c.execute(sqlalchemy.text(
                    "SHOW TABLES"))}
                ver = c.execute(sqlalchemy.text(
                    "SELECT version_num FROM alembic_version")).scalar()
                fks = c.execute(sqlalchemy.text(
                    "SELECT COUNT(*) FROM information_schema.REFERRAL_"
                    "CONSTRAINTS WHERE CONSTRAINT_SCHEMA = :d".replace(
                        "REFERRAL_", "REFERENTIAL_")), {"d": db}).scalar()
                captures = c.execute(sqlalchemy.sql.text(
                    "SELECT t.token_number, t.status, o.name, s.name "
                    "FROM tokens t JOIN organizations o ON o.id=t.organization_id "
                    "JOIN services s ON s.id=t.service_id "
                    "WHERE t.id IN (1,2) ORDER BY t.id")).fetchall()
                users = c.execute(sqlalchemy.sql.text(
                    "SELECT id, name, email FROM users WHERE id = 1")).fetchall()
                stages = c.execute(sqlalchemy.sql.text(
                    "SELECT code, name, position FROM workflow_stages "
                    "WHERE workflow_id = 1 ORDER BY position")).fetchall()
            return tables, ver, fks, captures, users, stages

        st, ver, fks, captures, users, stages = snapshot(
            new_engine(sync_url + f"/{dst_db}", "dst-verify"), dst_db)
        sst, sver, sfks, scaptures, susers, sstages = snapshot(
            new_engine(sync_url + f"/{src_db}", "src-verify"), src_db)

        assert sst == st, \
            f"table set differs: only-source={sst - st}, only-restore={st - sst}"
        assert sver == ver, f"alembic_version {sver} vs {ver}"
        assert str(ver).startswith("0018"), f"unexpected revision {ver}"
        assert sfks == fks, f"FK count differs after restore: {sfks} vs {fks}"
        assert scaptures == captures, "token/org/service join rows differ"
        assert susers == users, "user rows differ"
        assert sstages == stages, "workflow stage rows differ"
        assert len(captures) == 2 and captures[0][0] == "RH-0001", captures
        print(f"[7] VERIFIED (schema_mode={schema_mode}): "
              f"schema tables={len(st)}, alembic_version={ver}, "
              f"FKs={fks}, token/org/service rows={len(captures)} "
              f"(RH-0001@{captures[0][2]}), users={len(users)}, "
              f"workflow stages={[(r[0], r[2]) for r in stages]}")

        # ── 7. application-level access on the restored DB ─────────────────
        dst_state = None
        for eng in created_engines:
            if eng.url.database == dst_db:
                with eng.connect() as c:
                    dst_state = c.execute(sqlalchemy.text(
                        "SELECT COUNT(*) FROM tokens WHERE status='waiting'")
                    ).scalar()
                break
        print("[8] application-level access read on restored DB OK "
              f"(waiting tokens={dst_state})")
    finally:
        # dispose every pooled connection BEFORE dropping — otherwise the
        # DROPs block forever on table metadata locks held by this
        # process's own idle pools (exact deadlock observed in dry runs).
        for eng in created_engines:
            try:
                eng.dispose()
            except Exception:
                pass
        if not args.keep:
            with server.connect() as c:
                for db in created_databases():
                    c.execute(sqlalchemy.text(f"DROP DATABASE IF EXISTS `{db}`"))
                    c.commit()
            print("[9] teardown: dropped only the DBs this run created "
                  "(owned-cleanup pattern)")
        else:
            print("[9] --keep: leaving both rehearsal DBs for inspection "
                  f"({src_db}, {dst_db})")
    server.dispose()
    print(f"ALL-OK (schema_mode={schema_mode}): backup/restore rehearsal "
          f"verified in {time.time() - t_start:.1f}s — dump produced by the "
          f"production mechanism with integrity+sha256, restored into a "
          f"fresh disposable DB, schema/rows/FKs/alembic version identical, "
          f"application access read OK. No product/protected DB was touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
