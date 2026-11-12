"""WP2 (R7): end-to-end least-privilege migration verification.

Proves the full alembic chain 0001 -> 0018 runs cleanly under ONLY the
least-privilege application identity (skipline_app, from .env) — no root
fallback anywhere:

  1. CREATE DATABASE skipline_wps_privcheck_<stamp>  (as skipline_app — the
     wildcard-pattern GRANT from R2 makes this possible; probe evidence in
     wps2_evidence.json)
  2. alembic upgrade head  (subprocess with DB_NAME=<privcheck DB>)
  3. verify alembic_version == 0018 and table count == 65  (as skipline_app)
  4. DROP DATABASE  (as skipline_app — owned-only teardown)

Evidence: %LOCALAPPDATA%\\Temp\\opencode\\wp2\\wps2_privcheck_evidence.json
Secrets are never printed (DB_PASSWORD masked in any subprocess output).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
WP2_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Temp" / "opencode" / "wp2"
EVIDENCE = WP2_DIR / "wps2_privcheck_evidence.json"
EXPECTED_HEAD = "0018"
EXPECTED_TABLES = 65  # parity with the WP1 rehearsal evidence


def log(msg: str) -> None:
    print(f"[wp2-privcheck] {msg}", flush=True)


def load_env() -> dict:
    from dotenv import dotenv_values
    vals = dotenv_values(ROOT / ".env") or {}
    missing = [k for k in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD") if not vals.get(k)]
    if missing:
        raise RuntimeError(f"missing in .env: {missing}")
    if vals["DB_USER"] != "skipline_app":
        raise RuntimeError(
            f"refusing: DB_USER={vals['DB_USER']!r} is not the least-privilege "
            "identity skipline_app — R7 must run with zero root fallback")
    if vals["DB_HOST"] not in ("127.0.0.1", "localhost"):
        raise RuntimeError("DB_HOST is not loopback — R7 refused")
    return vals


def connect_app(vals: dict, database: str | None = None):
    return pymysql.connect(
        host="127.0.0.1", port=int(vals["DB_PORT"]), user=vals["DB_USER"],
        password=vals["DB_PASSWORD"], database=database,
        autocommit=True, charset="utf8mb4")


def main() -> int:
    evidence: dict = {"stamp": datetime.now(timezone.utc).isoformat()}
    vals = load_env()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    db = f"skipline_wps_privcheck_{stamp}"
    evidence["database"] = db
    evidence["identity"] = vals["DB_USER"]
    try:
        # 1) create the disposable DB as skipline_app (pattern GRANT)
        t0 = time.time()
        with connect_app(vals) as conn, conn.cursor() as c:
            c.execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 "
                      "COLLATE utf8mb4_unicode_ci")
        evidence["create_db_seconds"] = round(time.time() - t0, 2)
        log(f"created {db} as {vals['DB_USER']}")

        # 2) real alembic chain under the app identity
        env = dict(os.environ)
        env["DB_NAME"] = db  # override the .env DB_NAME for this run only
        log("alembic upgrade head (as skipline_app) ...")
        t0 = time.time()
        r = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900)
        out = (r.stdout + r.stderr).replace(vals["DB_PASSWORD"], "***")
        evidence["alembic_seconds"] = round(time.time() - t0, 2)
        if r.returncode != 0:
            raise RuntimeError("alembic upgrade head FAILED under "
                               f"{vals['DB_USER']}:\n" + out[-2500:])
        log(f"alembic chain complete in {evidence['alembic_seconds']}s")

        # 3) verify head version + table count as skipline_app
        with connect_app(vals, database=db) as conn, conn.cursor() as c:
            c.execute("SELECT version_num FROM alembic_version")
            ver = c.fetchall()[0][0]
            c.execute("SELECT COUNT(*) FROM information_schema.tables "
                      "WHERE table_schema=%s AND table_type='BASE TABLE'", (db,))
            tables = int(c.fetchone()[0])
        if ver != EXPECTED_HEAD:
            raise RuntimeError(f"alembic head mismatch: {ver} != {EXPECTED_HEAD}")
        if tables != EXPECTED_TABLES:
            raise RuntimeError(f"table count mismatch: {tables} != {EXPECTED_TABLES}")
        evidence["alembic_version"] = ver
        evidence["tables"] = tables
        log(f"verified: alembic_version={ver}, tables={tables}")

        # 4) owned teardown (also proves DROP-on-pattern for the app identity)
        with connect_app(vals) as conn, conn.cursor() as c:
            c.execute(f"DROP DATABASE `{db}`")
        log(f"dropped {db} (owned-only teardown)")
        evidence["teardown"] = "dropped"
        evidence["status"] = "PASS"
    except Exception as e:  # noqa: BLE001 — one-shot evidence tool
        msg = str(e)[:1200].replace(vals.get("DB_PASSWORD", "\x00"), "***")
        log(f"FAIL/BLOCKED: {msg}")
        evidence["status"] = "FAIL"
        evidence["error"] = msg
        # best-effort owned teardown
        try:
            with connect_app(vals) as conn, conn.cursor() as c:
                c.execute(f"DROP DATABASE IF EXISTS `{db}`")
            evidence["teardown"] = "dropped (after failure)"
        except Exception as te:  # noqa: BLE001
            log(f"[teardown] failed: {str(te)[:200]}")
    WP2_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    log(f"RESULT: {evidence['status']} — evidence: {EVIDENCE}")
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
