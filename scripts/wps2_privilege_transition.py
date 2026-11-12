"""WP2 (R0–R4): MySQL least-privilege transition driver.

Executes, in ONE process so generated secrets never need to touch disk
beyond the atomic .env update (prepare-then-rename):

  R0  full logical backup of the three protected databases (mysqldump)
  R2  provision skipline_app@localhost with strict least-privilege grants
      (skipline.* + disposable prefixes; zero global privileges), and
      empirically probe that wildcard-pattern GRANTs allow the app to
      CREATE/DROP disposable databases (needed by the acceptance harness
      and the alembic privcheck)
  R1  rotate root@localhost; verify the new credential works and the old
      compromised credential is rejected immediately
  R4  atomically rewrite .env (DB_USER/DB_PASSWORD -> skipline_app; add
      MYSQL_ROOT_PASSWORD for compose/admin use), preserving CRLF and all
      other keys, then re-read and verify (booleans only)

R8/R9 note: the residual transcript-leak redaction (old root password in
disclosed .out artifacts) runs FIRST in this same process — the value is
in memory here, and redacting before rotation removes the compromised
credential from disk entirely (order deviation from the task list is
documented in the WP2 report).

Safety:
  - no password value is ever printed or written to evidence (masked ***)
  - protected databases are only ever READ (R0 dump), never modified
  - the old root password is kept only in process memory for the
    old-credential-rejection check, then dropped

Evidence: %LOCALAPPDATA%\\Temp\\opencode\\wp2\\wps2_evidence.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import string
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
WP2_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Temp" / "opencode" / "wp2"
EVIDENCE = WP2_DIR / "wps2_evidence.json"

PROTECTED = ("skipline", "skipline_phase9_prodcheck", "skipline_synccheck")
BACKUP_DBS = list(PROTECTED)  # R0 covers exactly these three

APP_USER = "skipline_app"
APP_HOSTS = ("localhost",)  # compose's db is its own MySQL instance; host MySQL is loopback-only
GRANT_DBS = ("skipline", r"skipline_acceptance\_%", r"skipline_wps\_%", r"skipline_test\_%")
GRANT_PRIVS = ("SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, "
               "REFERENCES, CREATE TEMPORARY TABLES, LOCK TABLES")

MASK = "***"
fov: dict = {}
S: dict = {}  # in-process secret store — values NEVER printed or logged


def log(msg: str) -> None:
    print(f"[wp2] {msg}", flush=True)


def gen_password(length: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits  # URL/shell safe (no specials)
    return "".join(secrets.choice(alphabet) for _ in range(length))


def stage(name: str, fn):
    log(f"[{name}] start")
    t0 = time.time()
    result = fn() or {}
    result["seconds"] = round(time.time() - t0, 2)
    fov.setdefault("stages", {})[name] = result
    log(f"[{name}] done in {result['seconds']}s")
    return result


def load_env() -> dict:
    from dotenv import dotenv_values
    vals = dotenv_values(ENV_PATH) or {}
    missing = [k for k in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD") if not vals.get(k)]
    if missing:
        raise RuntimeError(f"missing in .env: {missing}")
    if vals["DB_HOST"] not in ("127.0.0.1", "localhost"):
        raise RuntimeError("DB_HOST is not loopback — WP2 transition refused")
    return vals


def root_connect(vals: dict):
    return pymysql.connect(
        host="127.0.0.1", port=int(vals["DB_PORT"]),
        user=vals["DB_USER"], password=vals["DB_PASSWORD"],
        autocommit=True, charset="utf8mb4")


# ── R8/R9 (early): residual transcript-leak redaction ───────────────────────

def redact_transcript_leaks(old_pw: str) -> dict:
    """Find files under %TEMP%\\opencode and the repo (excluding .git/venv)
    that contain the compromised root password and redact the value in
    place. Prints paths + redaction counts only — never the value."""
    targets: list[Path] = []
    for base, skip in ((WP2_DIR.parent, ("wp2",)), (ROOT, (".git", "venv", "node_modules"))):
        for p in base.rglob("*"):
            if p.is_dir():
                continue
            if any(part in skip for part in p.parts):
                continue
            if p.suffix.lower() in (".out", ".txt", ".log", ".md", ".json", ".xml"):
                targets.append(p)
    redacted: list[dict] = []
    for p in targets:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if old_pw.encode("utf-8") not in data:
            continue
        count = data.count(old_pw.encode("utf-8"))
        p.write_bytes(data.replace(old_pw.encode("utf-8"), b"***REDACTED-WP2***"))
        redacted.append({"path": str(p), "occurrences": count})
    if not redacted:
        return {"redacted_files": 0, "note": "no residual artifacts contain the old credential"}
    return {"redacted_files": len(redacted), "files": redacted}


# ── R0: pre-transition backup ────────────────────────────────────────────────

def r0_backup(vals: dict) -> dict:
    WP2_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = WP2_DIR / f"pre_wp2_backup_{stamp}.sql"
    mysqldump = Path(os.environ.get(
        "MYSQLDUMP_PATH",
        r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe"))
    if not mysqldump.exists():
        raise RuntimeError(f"mysqldump not found at {mysqldump}")
    env = dict(os.environ)
    env["MYSQL_PWD"] = vals["DB_PASSWORD"]  # keep the credential off the command line
    cmd = [
        str(mysqldump),
        "--host=127.0.0.1", f"--port={vals['DB_PORT']}",
        f"--user={vals['DB_USER']}",
        "--single-transaction", "--routines", "--events", "--triggers",
        "--set-gtid-purged=OFF",
        "--databases", *BACKUP_DBS,
        f"--result-file={out}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600, env=env)
    if r.returncode != 0:
        # stdout/stderr could theoretically echo parameters; mask defensively
        out_msg = (r.stdout + r.stderr).replace(vals["DB_PASSWORD"], MASK)
        raise RuntimeError("mysqldump failed:\n" + out_msg[-2000:])
    size = out.stat().st_size
    if size == 0:
        raise RuntimeError("backup file is empty")
    tail = out.read_text(encoding="utf-8", errors="replace")[-4096:]
    if "Dump completed" not in tail:
        raise RuntimeError("backup file lacks the mysqldump 'Dump completed' footer")
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    return {"path": str(out), "bytes": size, "databases": BACKUP_DBS,
            "sha256": sha, "footer": "Dump completed"}


# ── R2: least-privilege app account ─────────────────────────────────────────

def r2_provision(vals: dict, app_pw: str) -> dict:
    conn = root_connect(vals)
    try:
        with conn.cursor() as c:
            for host in APP_HOSTS:
                # Same plugin as root (caching_sha2_password; cryptography is
                # installed, so pymysql/aiomysql authenticate cleanly).
                # IF NOT EXISTS + ALTER makes this idempotent across reruns:
                # a previous run may have created the user with a lost
                # in-memory password; ALTER force-sets the current one.
                c.execute(
                    "CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                    (APP_USER, host, app_pw))
                c.execute("ALTER USER %s@%s IDENTIFIED BY %s",
                          (APP_USER, host, app_pw))
            # NOTE: the identifier is an in-code constant (no user input), so
            # it is inlined instead of parameterized — pymysql formats the
            # query with `sql % args`, and the wildcard `%` inside the db
            # pattern collides with that formatting.
            for grant_db in GRANT_DBS:
                for host in APP_HOSTS:
                    c.execute(
                        f"GRANT {GRANT_PRIVS} ON `{grant_db}`.* "
                        f"TO '{APP_USER}'@'{host}'")
            c.execute("FLUSH PRIVILEGES")
    finally:
        conn.close()
    return verify_grants(vals)


def verify_grants(vals: dict) -> dict:
    conn = root_connect(vals)
    try:
        with conn.cursor() as c:
            c.execute(f"SHOW GRANTS FOR '{APP_USER}'@'localhost'")
            grants = [r[0] for r in c.fetchall()]
    finally:
        conn.close()
    global_grants = [g for g in grants if re.search(r" ON (\*|`%`)\.\* ", g)
                     and not g.startswith("GRANT USAGE ON *.*")]
    if global_grants:
        raise RuntimeError(f"{APP_USER} has global privileges: {global_grants}")
    for banned in ("SUPER", "GRANT OPTION", "FILE", "PROCESS", "SHUTDOWN", "RELOAD"):
        for g in grants:
            if re.search(rf"\b{banned}\b", g):
                raise RuntimeError(f"{APP_USER} grant contains banned privilege {banned}: {g}")
    return {"grants": grants, "global_privileges": "none (USAGE only)",
            "banned_privileges": "none"}


def probe_pattern_ddl(vals: dict) -> dict:
    """Empirically confirm the wildcard-pattern grants let skipline_app
    CREATE/DROP disposable databases (the acceptance conftest and the R7
    privcheck depend on this)."""
    import socket
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    conn = pymysql.connect(
        host="127.0.0.1", port=int(vals["DB_PORT"]),
        user=APP_USER, password=S["app_pw"], autocommit=True)
    probed = []
    try:
        with conn.cursor() as c:
            for prefix in ("skipline_wps_probe_", "skipline_acceptance_probe_"):
                db = f"{prefix}{stamp}"
                c.execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                c.execute(f"DROP DATABASE `{db}`")
                probed.append(f"{prefix}*")
    finally:
        conn.close()
    return {"pattern_create_drop": "OK", "prefixes": probed}


# ── R1: root password rotation ──────────────────────────────────────────────

def r1_rotate_root(vals: dict, new_root_pw: str) -> dict:
    conn = root_connect(vals)
    try:
        with conn.cursor() as c:
            c.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (new_root_pw,))
            c.execute("FLUSH PRIVILEGES")
    finally:
        conn.close()
    # 1) the NEW credential must authenticate
    new_ok = True
    try:
        chk = pymysql.connect(host="127.0.0.1", port=int(vals["DB_PORT"]),
                              user="root", password=new_root_pw, autocommit=True)
        with chk.cursor() as c:
            c.execute("SELECT 1")
        chk.close()
    except pymysql.err.OperationalError:
        new_ok = False
    if not new_ok:
        raise RuntimeError("new root password failed authentication — refusing to proceed")
    # 2) the OLD (compromised) credential must be rejected
    old_rejected = False
    try:
        pymysql.connect(host="127.0.0.1", port=int(vals["DB_PORT"]),
                        user="root", password=vals["DB_PASSWORD"])
    except pymysql.err.OperationalError as e:
        old_rejected = (e.args[0] == 1045)
    if not old_rejected:
        raise RuntimeError("old root credential was NOT rejected — rotation not effective")
    return {"target": "root@localhost", "new_credential": "VERIFIED",
            "old_credential": "REJECTED (1045 Access denied)", "flush": "executed"}


# ── R4: atomic .env transition (prepare-then-commit) ────────────────────────

def prepare_env_update(new_user: str, new_db_pw: str, new_root_pw: str) -> bytes:
    text = ENV_PATH.read_bytes().decode("utf-8")  # CRLF preserved in the text
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    out_lines: list[str] = []
    seen = set()
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("DB_USER="):
            out_lines.append(f"DB_USER={new_user}")
            seen.add("DB_USER")
        elif stripped.startswith("DB_PASSWORD="):
            out_lines.append(f"DB_PASSWORD={new_db_pw}")
            seen.add("DB_PASSWORD")
        elif stripped.startswith("MYSQL_ROOT_PASSWORD="):
            out_lines.append(f"MYSQL_ROOT_PASSWORD={new_root_pw}")
            seen.add("MYSQL_ROOT_PASSWORD")
        else:
            out_lines.append(line)
    if "MYSQL_ROOT_PASSWORD" not in seen:
        # insert after the DB block (after DB_PASSWORD) or at the end
        idx = next((i for i, l in enumerate(out_lines)
                    if l.startswith("DB_PASSWORD=")), len(out_lines) - 1)
        out_lines.insert(idx + 1,
                         "# Docker compose: required for the db/backup services (R4)"
                         + nl + f"MYSQL_ROOT_PASSWORD={new_root_pw}")
    return nl.join(out_lines).encode("utf-8")


def r4_env_update(vals: dict, app_pw: str, new_root_pw: str) -> dict:
    new_bytes = prepare_env_update(APP_USER, app_pw, new_root_pw)
    tmp = ENV_PATH.with_name(".env.wp2new")
    tmp.write_bytes(new_bytes)
    # caller commits AFTER the ALTER USER succeeds (see main)
    return {"prepared": str(tmp), "keys_rewritten": ["DB_USER", "DB_PASSWORD"],
            "keys_added": ["MYSQL_ROOT_PASSWORD"], "other_keys": "preserved verbatim"}


def commit_env_update() -> dict:
    tmp = ENV_PATH.with_name(".env.wp2new")
    os.replace(tmp, ENV_PATH)
    # re-read and verify — booleans only, values never printed
    from dotenv import dotenv_values
    v = dotenv_values(ENV_PATH) or {}
    checks = {
        "DB_USER": v.get("DB_USER") == APP_USER,
        "DB_PASSWORD": bool(v.get("DB_PASSWORD")),
        "MYSQL_ROOT_PASSWORD": bool(v.get("MYSQL_ROOT_PASSWORD")),
    }
    if not all(checks.values()):
        raise RuntimeError(f".env verification failed: {checks}")
    return {"committed": True, "checks": checks,
            "line_endings": "CRLF preserved", "secret_values": MASK}


# ── post-verification ───────────────────────────────────────────────────────

def post_verify(vals: dict, new_root_pw: str) -> dict:
    # protected DBs intact (admin view, using the NEW root credential from .env)
    conn = pymysql.connect(host="127.0.0.1", port=int(vals["DB_PORT"]),
                           user="root", password=new_root_pw, autocommit=True)
    try:
        with conn.cursor() as c:
            c.execute("SHOW DATABASES")
            dbs = {r[0] for r in c.fetchall()}
    finally:
        conn.close()
    missing = [d for d in PROTECTED if d not in dbs]
    if missing:
        raise RuntimeError(f"protected databases missing after transition: {missing}")
    # runtime connectivity as skipline_app against the app DB
    conn = pymysql.connect(host="127.0.0.1", port=int(vals["DB_PORT"]),
                           user=APP_USER, password=S["app_pw"], database="skipline",
                           autocommit=True)
    try:
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM organizations")
            orgs = int(c.fetchone()[0])
    finally:
        conn.close()
    return {"protected_databases": "all present", "app_user_runtime_read":
            {"database": "skipline", "organizations_count": orgs}}


def main() -> int:
    evidence: dict = {"stamp": datetime.now(timezone.utc).isoformat(),
                      "baseline_head": "c2fa863e38643f148cbf8f91b7222a2a3345c22f"}
    secrets_seen: list[str] = []
    try:
        vals = load_env()
        S["app_pw"] = gen_password()
        S["root_pw"] = gen_password()
        secrets_seen = [S["app_pw"], S["root_pw"], vals["DB_PASSWORD"]]
        stage("r8_redact_transcript_leaks", lambda: redact_transcript_leaks(vals["DB_PASSWORD"]))
        stage("r0_backup", lambda: r0_backup(vals))
        stage("r2_provision_app_user", lambda: r2_provision(vals, S["app_pw"]))
        stage("r2_probe_pattern_ddl", lambda: probe_pattern_ddl(vals))
        stage("r1_rotate_root", lambda: r1_rotate_root(vals, S["root_pw"]))
        stage("r4_prepare_env", lambda: r4_env_update(vals, S["app_pw"], S["root_pw"]))
        # commit the .env ONLY after ALTER USER succeeded
        stage("r4_commit_env", commit_env_update)
        stage("post_verify", lambda: post_verify(vals, S["root_pw"]))
        evidence["status"] = "PASS"
    except Exception as e:  # noqa: BLE001 — one-shot transition tool
        msg = str(e)[:800]
        for pw in secrets_seen:
            if pw:
                msg = msg.replace(pw, MASK)
        log(f"FAIL/BLOCKED: {msg}")
        evidence["status"] = "FAIL"
        evidence["error"] = msg
    evidence["stages"] = dict(fov.get("stages", {}))
    WP2_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    log(f"RESULT: {evidence['status']} — evidence: {EVIDENCE}")
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
