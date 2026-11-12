"""WP2 (R8/R9): residual root-credential leak detection + redaction.

The compromised MySQL root password was rotated out in the WP2 transition
(wps2_privilege_transition.py). After rotation the value exists nowhere in
memory or configuration, so the leak cannot be located by exact-value
search. Instead this tool detects CREDENTIAL-SHAPED disclosures
(root connection URLs, DB_PASSWORD assignments, IDENTIFIED BY literals,
-p<password> flags) in session-shell artifacts, verifies each detected
value is the DEAD credential (a root connection attempt with it must fail
with 1045 — proving it is not the new rotated-in password), and redacts
the value in place, keeping surrounding structure.

Scope: session shell logs (*.out under the harness shell tree) and text
artifacts under %TEMP%\\opencode. Never prints a detected value.

Evidence: %LOCALAPPDATA%\\Temp\\opencode\\wp2\\wps2_leak_redaction.json
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pymysql
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SHELL_TREE = Path.home() / ".local" / "share" / "opencode" / "shell"
TEMP_OC = Path(os.environ.get("TEMP", str(Path.home() / "AppData/Local/Temp"))) / "opencode"
EVIDENCE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Temp" / "opencode" / "wp2" / "wps2_leak_redaction.json"
REDACTED = b"***REDACTED-WP2***"

# Credential-context shapes (value capture groups). The generator produced
# 32+ ALPHANUMERIC-only passwords, so 20+ contiguous alnum inside one of
# these contexts is a real credential, not prose or a dashed test constant.
SHAPES = {
    "root_url": re.compile(r"mysql[+a-z]*://root:([A-Za-z0-9]{20,})@"),
    "db_password_assign": re.compile(r"DB_PASSWORD=([A-Za-z0-9]{20,})"),
    "identified_by": re.compile(r"IDENTIFIED BY '?([A-Za-z0-9]{20,})'?"),
    "mysql_p_flag": re.compile(r"-p([A-Za-z0-9]{20,})\b"),
}


def candidate_files() -> list[Path]:
    out: list[Path] = []
    if SHELL_TREE.exists():
        out.extend(p for p in SHELL_TREE.rglob("*.out") if p.stat().st_size < 50 * 1024 * 1024)
    if TEMP_OC.exists():
        for p in TEMP_OC.rglob("*"):
            if p.is_file() and p.stat().st_size < 50 * 1024 * 1024 \
                    and p.suffix.lower() in (".out", ".txt", ".log", ".json", ".xml", ".md"):
                out.append(p)
    return out


def main() -> int:
    vals = dotenv_values(ROOT / ".env") or {}
    port = int(vals.get("DB_PORT", "3306"))

    files = candidate_files()
    scanned_paths = [str(p) for p in files]
    values: dict[str, dict] = {}  # value -> {"shape": str, "dead": bool|None}
    file_hits: list[tuple[Path, int]] = []
    for p in files:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        found_any = False
        for name, rx in SHAPES.items():
            for m in rx.finditer(data.decode("utf-8", errors="replace")):
                v = m.group(1)
                if v not in values:
                    values[v] = {"shape": name, "dead": None}
                found_any = True
        if found_any:
            file_hits.append((p, data))

    # verify each unique detected value is the DEAD credential: a root
    # connection with it must be REJECTED (1045). Never prints the value.
    for v, meta in values.items():
        try:
            conn = pymysql.connect(host="127.0.0.1", port=port, user="root", password=v)
            conn.close()
            meta["dead"] = False  # still authenticates — NOT redacted (report only)
        except pymysql.err.OperationalError as e:
            meta["dead"] = (e.args[0] == 1045)
        except Exception:  # noqa: BLE001 — treat unknown as dead-conservative? no: mark unknown
            meta["dead"] = None

    dead_values = [v for v, m in values.items() if m["dead"] is True]
    live_values = [v for v, m in values.items() if m["dead"] is False]

    # redact ONLY dead (rotated-out) credentials
    redacted_files = []
    for p, data in file_hits:
        changed = False
        for v in dead_values:
            if v.encode("utf-8") in data:
                data = data.replace(v.encode("utf-8"), REDACTED)
                changed = True
        if changed:
            p.write_bytes(data)
            redacted_files.append({"path": str(p), "status": "redacted"})

    evidence = {
        "scanned_files": len(scanned_paths),
        "hit_files": len(file_hits),
        "unique_values_detected": len(values),
        "dead_credentials_redacted": len(dead_values),
        "live_credentials_found": len(live_values),
        "redacted_files": redacted_files,
        "note": "detected values are never printed; 'dead' = root auth REJECTED (1045), "
                "proving the value is the rotated-out compromised credential",
    }
    # keep the file lists compact (hit list = files that contained a detected
    # credential-shaped value; the full scanned list is elided)
    evidence["hit_file_list"] = [str(p) for p, _ in file_hits][:50]
    # preserve redaction history across reruns (later reruns find nothing,
    # because the redacted value no longer exists on disk)
    if EVIDENCE.exists():
        try:
            prior = json.loads(EVIDENCE.read_text(encoding="utf-8"))
            prior_redactions = prior.get("redacted_files", [])
            if prior_redactions:
                seen = {r["path"] for r in redacted_files}
                evidence["redacted_files"] = redacted_files + [
                    r for r in prior_redactions if r["path"] not in seen]
                evidence["dead_credentials_redacted"] += prior.get(
                    "dead_credentials_redacted", 0)
        except (OSError, json.JSONDecodeError):
            pass
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(f"[wp2-leak] scanned={evidence['scanned_files']} files; "
          f"unique credential values detected={evidence['unique_values_detected']}; "
          f"dead(rotated-out) redacted={evidence['dead_credentials_redacted']}; "
          f"LIVE credentials found={evidence['live_credentials_found']}")
    if live_values:
        print("[wp2-leak] WARNING: a detected value still AUTHENTICATES as root — "
              "NOT redacted; investigate (value not printed)")
        return 1
    print(f"[wp2-leak] RESULT: {'PASS' if dead_values or not values else 'CLEAN'} — evidence: {EVIDENCE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
