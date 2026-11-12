"""WP1 Task 3 rehearsal: GENUINE production-path backup/restore.

Unlike scripts/backup_restore_rehearsal.py (the earlier qualification run),
this driver exercises the REAL production mechanisms end to end:

  source DB (skipline_wps_backup_<stamp>)
    <- REAL alembic chain (python -m alembic upgrade head, async/greenlet path)
    <- representative seeding
    <- REAL scripts/backup.sh  (mysqldump --single-transaction ... | gzip)
    <- REAL scripts/restore.sh (documented restore semantics, scratch target)
    <- parity verification (tables, alembic_version, FKs, indexes, rows)
    <- application-level reads (app's own async engine + real uvicorn /health)
    <- owned teardown

NO create_all fallback exists here: if the real alembic path cannot run in
this environment the rehearsal reports BLOCKED with exact evidence.

Security rules enforced:
  - credentials are loaded from .env in-process, never printed (mask on);
  - source/restore DB names are uniquely stamped and are ONLY the ones
    created and dropped by this script;
  - DB_HOST must be loopback.

Output: JSON evidence to %TEMP%/opencode/wp1/rehearsal_evidence.json plus a
human summary on stdout.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(os.environ.get("TEMP", "/tmp")) / "opencode" / "wp1"
TMP.mkdir(parents=True, exist_ok=True)
EVIDENCE = TMP / "rehearsal_evidence.json"

PROTECTED = {"skipline", "skipline_phase9_prodcheck", "skipline_synccheck"}
STAMP = time.strftime("%Y%m%d%H%M%S")
BACKUP_DB = f"skipline_wps_backup_{STAMP}"
RESTORE_DB = f"skipline_wps_restore_{STAMP}"

# alembic head at time of writing (WP1); asserted against installed files.
EXPECTED_HEAD = "0018"

fov = {}


def log(msg: str) -> None:
    print(f"[wp1-rehearsal] {msg}", flush=True)


def load_env() -> None:
    # Parse like pydantic-settings (python-dotenv): inline comments are
    # stripped there; injecting a raw comment-bearing string as an OS var
    # would shadow the correct file value and break Settings validation.
    # Only missing keys are injected.
    from dotenv import dotenv_values
    vals = dotenv_values(ROOT / ".env") or {}
    for k, v in vals.items():
        if v is not None and k not in os.environ:
            os.environ[k] = v
    for k in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD"):
        if not os.environ.get(k):
            raise RuntimeError(f"{k} missing from .env — rehearsal refused")


def server_connect():
    host = os.environ.get("DB_HOST") or "127.0.0.1"
    port = int(os.environ.get("DB_PORT") or "3306")
    if host not in ("127.0.0.1", "localhost"):
        raise RuntimeError(f"DB_HOST={host} is not loopback — rehearsal refused")
    return pymysql.connect(
        host=host, port=port,
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        autocommit=True)


def db_query(conn, sql, args=None):
    """pymysql has no Connection.execute — go through a cursor."""
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def stage(name, fn):
    t0 = time.time()
    log(f"[{name}] start")
    result = fn()
    dt = time.time() - t0
    log(f"[{name}] done in {dt:.1f}s")
    fov.setdefault("stages", {})[name] = {"seconds": round(dt, 2), **result}
    return result


def create_backup_db():
    with server_connect() as c:
        existing = {r[0] for r in db_query(c, "SHOW DATABASES")}
        for bad in (BACKUP_DB, RESTORE_DB):
            if bad in existing:
                raise RuntimeError(f"{bad} already exists — refusing")
            if bad in PROTECTED:
                raise RuntimeError(f"name collision with protected DB: {bad}")
        db_query(
            c,
            f"CREATE DATABASE `{BACKUP_DB}` CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci")
    return {"db": BACKUP_DB}


def run_real_alembic():
    env = dict(os.environ)
    env["DB_NAME"] = BACKUP_DB
    env["APP_ENV"] = env.get("APP_ENV") or "development"
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900)
    tail = (r.stdout + r.stderr).replace(os.environ["DB_PASSWORD"], "***")
    if r.returncode != 0:
        if ("Application Control policy" in tail
                or ("greenlet" in tail.lower() and "dll load failed" in tail.lower())):
            raise RuntimeError(
                "BLOCKED: WDAC/greenlet blocked the REAL async alembic path.\n"
                + tail[-1500:])
        raise RuntimeError("REAL alembic upgrade head failed:\n" + tail[-2000:])
    with server_connect() as c:
        ver = db_query(
            c, f"SELECT version_num FROM `{BACKUP_DB}`.alembic_version")[0][0]
    if ver != EXPECTED_HEAD:
        raise RuntimeError(f"alembic head mismatch: {ver} != {EXPECTED_HEAD}")
    return {"version": ver, "mode": "real-alembic-async-chain"}


NOT_NULL_SYNTH = {
    "tinyint": 1, "smallint": 1, "mediumint": 1, "int": 1, "bigint": 1,
    "datetime": "NOW()", "timestamp": "NOW()",
}


def _synth_missing_notnull(conn, db: str, table: str,
                           base: dict) -> tuple[list, list]:
    """Return (col_list, value_list) = explicit base values plus synthesized
    values for NOT NULL columns without defaults. Values are inlined with
    proper binding (parameters) — never interpolated user content."""
    with conn.cursor() as c:
        c.execute(
            "SELECT column_name, data_type, column_type, is_nullable, "
            "column_default, extra FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s", (db, table))
        rows = c.fetchall()
    cols, vals = [], []
    for name, dtype, ctype, nullable, default, extra in rows:
        if name in base:
            cols.append(name)
            vals.append(base[name])
            continue
        if "auto_increment" in (extra or ""):
            # NEVER synthesize the PK: an explicit value does not update
            # LAST_INSERT_ID(), so the caller would read 0 and any child
            # row referencing it would break on its FK (run-3 evidence).
            continue
        if nullable == "NO" and default is None:
            if ctype.startswith("enum("):
                v = ctype[6:ctype.find(",", 6) if "," in ctype[6:] else -1]
                v = v.rstrip(")").strip("'")
            elif dtype == "set":
                v = ""
            elif dtype in ("datetime", "timestamp"):
                v = "{{NOW}}"
            elif dtype == "date":
                v = "{{TODAY}}"
            elif dtype == "decimal":
                v = 1
            elif dtype == "json":
                v = "{}"
            elif dtype.endswith(("int",)):
                v = 1
            elif dtype in ("float", "double"):
                v = 1
            else:  # varchar/text/char/blob
                v = ("WH-" + name)[:96]
                m = re.search(r"\((\d+)\)", ctype)
                if m:  # column-length aware (String(5) etc.)
                    v = v[: int(m.group(1))]
            cols.append(name)
            vals.append(v)
    return cols, vals


def _inline(v) -> str:
    if v == "{{NOW}}":
        return "NOW()"
    if v == "{{TODAY}}":
        return "CURDATE()"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def seed_representative_rows() -> dict:
    """Insert representative production-shaped rows (same values the
    qualified rehearsal used, e.g. tokens RH-0001/RH-0002)."""
    conn = server_connect()
    try:
        with conn.cursor() as c:
            c.execute("SHOW TABLES FROM `%s`" % BACKUP_DB)
            tables = {r[0] for r in c.fetchall()}
            if "tokens" not in tables or "organizations" not in tables \
                    or "services" not in tables or "counters" not in tables:
                raise RuntimeError(
                    f"expected core tables missing from {BACKUP_DB}: {sorted(tables)[:8]}…")

            def insert(table, base):
                cols, vals = _synth_missing_notnull(conn, BACKUP_DB, table, base)
                c.execute(
                    f"INSERT INTO `{BACKUP_DB}`.{table} "
                    f"({', '.join(cols)}) VALUES ({', '.join(_inline(v) for v in vals)})")
                c.execute("SELECT LAST_INSERT_ID()")
                return int(c.fetchall()[0][0])

            org_id = insert("organizations", {"name": "Rehearsal Clinic",
                                              # 0015 reconciled status columns to
                                              # UPPERCASE MySQL ENUMs (orgstatus
                                              # = PENDING/APPROVED/REJECTED/
                                              # SUSPENDED) — lowercase values
                                              # are data-truncation errors.
                                              "status": "APPROVED"})
            svc_id = insert("services", {"organization_id": org_id,
                                         "name": "General Consultation",
                                         "is_active": 1})
            counter_id = insert("counters", {"organization_id": org_id,
                                             "counter_name": "Rehearsal Counter 1",
                                             "token_prefix": "RH",
                                             "status": "ACTIVE"})
            seeded: dict = {}
            for n in (1, 2):
                insert("tokens", {
                    "organization_id": org_id,
                    "service_id": svc_id,
                    "counter_id": counter_id,
                    "token_number": f"RH-{n:04d}",
                    "status": "WAITING",
                })
            seeded["tokens"] = ["RH-0001", "RH-0002"]
            if "service_workflows" in tables and "workflow_stages" in tables:
                wf_id = insert("service_workflows", {
                    "organization_id": org_id, "service_id": svc_id,
                    "name": "Rehearsal Flow", "is_active": 1})
                for pos, sname in ((1, "registration"), (2, "consultation")):
                    insert("workflow_stages", {
                        "workflow_id": wf_id, "name": sname, "code": sname,
                        "position": pos})
                seeded["workflow_stages"] = ["registration", "consultation"]
        return {"org_id": org_id, "seeded": seeded}
    finally:
        conn.close()


def run_real_backup() -> dict:
    """Execute the REAL scripts/backup.sh under WSL bash (WSL interop runs
    the Windows mysqldump.exe found via PATH; loopback is correct because
    the dump process itself is a Windows process)."""
    out_dir = TMP / "backup_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir_wsl = str(out_dir).replace("\\", "/").replace("C:/", "/mnt/c/")
    root_wsl = str(ROOT).replace("\\", "/").replace("C:/", "/mnt/c/")
    wrapper = TMP / "wp1_run_backup.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        # .env is CRLF on Windows: strip \r into a temp copy before
        # sourcing (never modify .env itself) — otherwise every value
        # carries a trailing \r and DB auth fails.
        f'tr -d \'\\r\' < "{root_wsl}/.env" > /tmp/wp1_env_$$.sh\n'
        "set -a\n. /tmp/wp1_env_$$.sh\nset +a\n"
        "rm -f /tmp/wp1_env_$$.sh\n"
        # WSL interop resolves mysqldump.exe but NOT bare "mysqldump"
        # (no .exe suffix search) — shim the production binary names
        # onto PATH so the REAL scripts/backup.sh runs unmodified.
        # NOTE: printf format must be SINGLE-quoted in the shell — a
        # double-quoted format would expand "$@" to the WRAPPER's (empty)
        # args and the shim would forward an empty string instead of
        # --user/--password (run-10 evidence: ODBC/no-password auth fail).
        "mkdir -p /tmp/wp1_shim_$$\n"
        'for b in mysqldump mysql mysqladmin; do\n'
        '  printf \'#!/bin/sh\\nexec "%s/%s.exe" "$@"\\n\' '
        '"/mnt/c/Program Files/MySQL/MySQL Server 8.0/bin" "$b" '
        "> /tmp/wp1_shim_$$/$b\n"
        "  chmod +x /tmp/wp1_shim_$$/$b\n"
        "done\n"
        'export PATH="/tmp/wp1_shim_$$:$PATH"\n'
        f'export BACKUP_DIR="{out_dir_wsl}"\n'
        f'export DB_NAME="{BACKUP_DB}"\n'
        f'sh "{root_wsl}/scripts/backup.sh"\n'
        "rm -rf /tmp/wp1_shim_$$\n",
        encoding="utf-8", newline="\n")
    r = subprocess.run(
        # WSL bash needs the /mnt/c/ form of the wrapper path — a Windows
        # path is mangled (backslashes dropped) and bash reports
        # "No such file or directory" (run-5 evidence).
        ["bash", str(wrapper).replace("\\", "/").replace("C:/", "/mnt/c/")],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout + r.stderr).replace(os.environ["DB_PASSWORD"], "***")
    if r.returncode != 0:
        raise RuntimeError("REAL scripts/backup.sh failed:\n" + out[-2500:])
    artifacts = sorted(out_dir.glob(f"{BACKUP_DB}_*.sql.gz"))
    if not artifacts:
        raise RuntimeError("backup.sh produced no artifact: " + out[-1500:])
    art = artifacts[-1]
    size = art.stat().st_size
    import hashlib
    sha = hashlib.sha256(art.read_bytes()).hexdigest()
    return {"artifact": art.name, "bytes": size, "sha256": sha, "mode": "real-scripts/backup.sh"}


def run_real_restore() -> dict:
    root_wsl = str(ROOT).replace("\\", "/").replace("C:/", "/mnt/c/")
    art = sorted((TMP / "backup_out").glob(f"{BACKUP_DB}_*.sql.gz"))[-1]
    art_wsl = str(art).replace("\\", "/").replace("C:/", "/mnt/c/")
    wrapper = TMP / "wp1_run_restore.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        # Same CRLF handling as the backup wrapper (never modify .env).
        f'tr -d \'\\r\' < "{root_wsl}/.env" > /tmp/wp1_env_$$.sh\n'
        "set -a\n. /tmp/wp1_env_$$.sh\nset +a\n"
        "rm -f /tmp/wp1_env_$$.sh\n"
        # Same interop shim as the backup wrapper (restore.sh uses `mysql`).
        "mkdir -p /tmp/wp1_shim_$$\n"
        'for b in mysqldump mysql mysqladmin; do\n'
        '  printf \'#!/bin/sh\\nexec "%s/%s.exe" "$@"\\n\' '
        '"/mnt/c/Program Files/MySQL/MySQL Server 8.0/bin" "$b" '
        "> /tmp/wp1_shim_$$/$b\n"
        "  chmod +x /tmp/wp1_shim_$$/$b\n"
        "done\n"
        'export PATH="/tmp/wp1_shim_$$:$PATH"\n'
        f'export DB_NAME="{BACKUP_DB}"\n'
        f'sh "{root_wsl}/scripts/restore.sh" "{art_wsl}" "{RESTORE_DB}"\n'
        "rm -rf /tmp/wp1_shim_$$\n",
        encoding="utf-8", newline="\n")
    r = subprocess.run(
        ["bash", str(wrapper).replace("\\", "/").replace("C:/", "/mnt/c/")],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout + r.stderr).replace(os.environ["DB_PASSWORD"], "***")
    if r.returncode != 0:
        raise RuntimeError("REAL scripts/restore.sh failed:\n" + out[-2500:])
    return {"target": RESTORE_DB, "mode": "real-scripts/restore.sh (scratch validation mode)"}


def parity(db: str) -> dict:
    with server_connect() as c:
        tables_q = (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name")
        idx_q = (
            "SELECT table_name, index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index) "
            "FROM information_schema.statistics WHERE table_schema=%s AND index_name<>'PRIMARY' "
            "GROUP BY table_name, index_name ORDER BY table_name, index_name")
        fk_q = (
            "SELECT CONSTRAINT_NAME, TABLE_NAME, REFERENCED_TABLE_NAME FROM "
            "information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=%s "
            "ORDER BY CONSTRAINT_NAME")
        ver_q = f"SELECT version_num FROM `{db}`.alembic_version"
        tok_q = (
            f"SELECT t.token_number, o.name, s.name FROM `{db}`.tokens t "
            f"JOIN `{db}`.organizations o ON o.id=t.organization_id "
            f"JOIN `{db}`.services s ON s.id=t.service_id ORDER BY t.token_number")
        cnt_q = f"SELECT COUNT(*) FROM `{db}`.tokens WHERE status='waiting'"
        res = {}
        for label, q in (("tables", tables_q), ("indexes", idx_q), ("fks", fk_q)):
            res[label] = [tuple(map(str, r)) for r in db_query(c, q, (db,))]
        # tok_q already interpolates the schema name (f-string) — no %s
        # placeholder, so it must NOT receive args (pymysql would attempt
        # sql % args and fail; run-11 evidence).
        res["token_rows"] = [tuple(map(str, r)) for r in db_query(c, tok_q)]
        res["alembic_version"] = db_query(c, ver_q)[0][0]
        res["waiting_tokens"] = int(db_query(c, cnt_q)[0][0])
    return res


def verify_parity():
    src = parity(BACKUP_DB)
    dst = parity(RESTORE_DB)
    checks = {}
    for k in ("tables", "indexes", "fks", "token_rows",
              "alembic_version", "waiting_tokens"):
        checks[k] = "MATCH" if src[k] == dst[k] else "MISMATCH"
        if src[k] != dst[k]:
            raise RuntimeError(f"parity MISMATCH [{k}]: src={src[k][:5]}… dst={dst[k][:5]}…")
    return {
        "checks": checks,
        "tables": len(dst["tables"]), "indexes": len(dst["indexes"]),
        "fks": len(dst["fks"]), "alembic_version": dst["alembic_version"],
        "token_rows": dst["token_rows"],
    }


def app_level_read() -> dict:
    """Application-level read: the app's OWN async engine reads from the
    restored DB, and a REAL uvicorn process boots against it (/health)."""
    sys.path.insert(0, str(ROOT))
    os.environ["DB_NAME"] = RESTORE_DB
    from app.config import settings  # noqa: E402  (app identity, unmodified)
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text  # noqa: E402
    import asyncio

    async def _read():
        eng = create_async_engine(settings.async_database_url)
        async with eng.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT COUNT(*) FROM tokens WHERE status='waiting'"))).scalar()
            stages = (await conn.execute(text(
                "SELECT name FROM workflow_stages ORDER BY position"))).scalars().all()
            ver = (await conn.execute(text(
                "SELECT version_num FROM alembic_version"))).scalar()
        await eng.dispose()
        return {"waiting_tokens": int(waiting), "workflow_stages": list(stages),
                "alembic_version": ver}

    orm = asyncio.run(_read())

    s = socket.socket(); s.bind(("127.0.0.1", 0)); app_port = s.getsockname()[1]; s.close()
    env = dict(os.environ); env["DB_NAME"] = RESTORE_DB; env["APP_ENV"] = "development"
    env["LOG_DIR"] = ""
    uv_log = TMP / f"rehearsal_uvicorn_{STAMP}.log"
    # stdout must NOT be an unread PIPE in this environment: a piping-on
    # froze the uvicorn child mid-boot reproducibly during WP1 acceptance
    # diagnostics (PIPE 3/3 freeze; file redirect 1/1 healthy).
    with open(uv_log, "w", encoding="utf-8") as uv_out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(app_port)],
            cwd=str(ROOT), env=env,
            stdout=uv_out, stderr=subprocess.STDOUT)
        healthy = False
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                uv_out.flush()
                tail = uv_log.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise RuntimeError(
                    "real uvicorn exited during startup:\n" + tail)
            try:
                u = urllib.request.urlopen(
                    f"http://127.0.0.1:{app_port}/health", timeout=2)
                if u.status == 200:
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        proc.kill()
    if not healthy:
        uv_out.flush()
        tail = uv_log.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeError(
            "real uvicorn failed to reach /health on the restored DB:\n" + tail)
    return {"uvicorn_health": 200, "orm_read": orm,
            "mode": "app-async-engine + real uvicorn"}


def teardown() -> dict:
    dropped = []
    for db in (BACKUP_DB, RESTORE_DB):
        if db in PROTECTED or not (db.startswith("skipline_wps_")):
            raise RuntimeError(f"teardown guard refused: {db}")
        with server_connect() as c:
            db_query(c, f"DROP DATABASE IF EXISTS `{db}`")
        dropped.append(db)
    return {"dropped": dropped, "guard": "skipline_wps_* + stamp, protected names excluded"}


def main() -> int:
    evidence: dict = {
        "stamp": STAMP, "backup_db": BACKUP_DB, "restore_db": RESTORE_DB,
        "expected_head": EXPECTED_HEAD,
        "mysql_client": "Windows mysqldump.exe via WSL interop",
    }
    try:
        load_env()
        stage("create_backup_db", create_backup_db)
        stage("real_alembic_chain", run_real_alembic)
        stage("seed_representative_rows", seed_representative_rows)
        stage("real_backup_script", run_real_backup)
        stage("real_restore_script", run_real_restore)
        stage("parity_verification", verify_parity)
        stage("app_level_read", app_level_read)
        foss = stage("teardown", teardown)
        status = "PASS"
    except Exception as e:  # noqa: BLE001 — one-shot evidence tool
        log(f"FAIL/BLOCKED: {str(e)[:800]}")
        status = "BLOCKED" if "BLOCKED:" in str(e) else "FAIL"
        evidence["error"] = str(e).replace(
            os.environ.get("DB_PASSWORD", "\x00"), "***")
        try:
            stage("teardown", teardown)
        except Exception as te:  # noqa: BLE001
            log(f"[teardown] failed after error: {te}")
    evidence["status"] = status
    # Persist the per-stage details (parity counts, artifact sha256,
    # seeded rows, teardown list) — without this the PASS verdict has no
    # structured evidence beyond the console log.
    evidence["stages"] = dict(fov.get("stages", {}))
    EVIDENCE.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    log(f"RESULT: {status} — evidence written to {EVIDENCE}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
