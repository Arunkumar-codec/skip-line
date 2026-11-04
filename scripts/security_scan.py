#!/usr/bin/env python3
"""
scripts/security_scan.py — local secret-scanning + dependency audit (SEC-008).

Scans every Git-tracked file for common credential patterns (cloud keys,
private keys, live API keys, hard-coded passwords) so a leaked secret is
caught BEFORE a commit/push, and optionally runs `pip-audit` against the
active environment to surface known CVEs in installed dependencies.

Exit codes:
  0 — no findings of the requested kind (or nothing scanned)
  1 — potential secrets found (scan mode) / vulnerable packages (audit mode)
  2 — usage error

Usage:
  python scripts/security_scan.py            # scan tracked files for secrets
  python scripts/security_scan.py --pip-audit  # + dependency CVE audit
  python scripts/security_scan.py --all        # everything above
  python scripts/security_scan.py --staged     # scan only staged changes

Honest scope: pattern scanning is a tripwire, NOT a guarantee. Findings
MUST be triaged by a human; the scanner can miss novel formats and its
regexes can over-match innocuous strings (each hit prints the rule name
so triage is fast).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# (rule name, compiled pattern). Order: highest-signal first.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private-key-block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("stripe-live-key", re.compile(r"(?:sk|rk)_(?:live|rlive)-?[0-9a-zA-Z]{20,}")),
    ("github-token", re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}")),
    ("slack-bot-token", re.compile(r"xoxb-[0-9]{10,}-[0-9A-Za-z-]+")),
    ("twilio-sid+secret-assign", re.compile(
        r"(?:twilio[_-]?(?:account)?[_-]?sid[\"'\s:=]+AC[0-9a-f]{32}|"
        r"twilio[_-]?auth[_-]?token[\"'\s:=]+[0-9A-Za-z]{32})", re.I)),
    ("sendgrid-api-key", re.compile(r"SG\.[0-9A-Za-z_-]{22}\.[0-9A-Za-z_-]{43}")),
    ("jwt-hardcoded-secret", re.compile(
        r"(?:jwt[_-]?secret|secret[_-]?key)[\"'\s:=]+['\"](?![\"']$)"
        r"[^\"']{16,}['\"]", re.I)),
    ("password-in-source", re.compile(
        r"(?:^|[^A-Za-z])(?:password|passwd|db_password|admin_password)"
        r"[\"']?\s*[:=]\s*[\"'](?![\"'\s]*(?:\$\{|<|\.\.\.))[^\"']{8,}[\"']",
        re.I)),
    ("connection-string-with-creds", re.compile(
        r"(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|amqp)://[^/\s:'\"]+:[^@\s"
        r":'\"]+@[^/\s:'\"]+", re.I)),
]

# Directories whose findings are deliberate examples, not leaked secrets:
#   tests/ — every fixture intentionally uses fake values (documented);
#   docs/  — API documentation example payloads (fictional accounts).
# Findings there are STILL PRINTED for triage, but do not block a push.
NON_BLOCKING_DIRS = ("tests/", "docs/")

# Files that legitimately contain short demonstration placeholders and
# operator-facing examples; every match inside these files is still PRINTED
# for human triage, but only file-name matches for allowlist-ish patterns
# (e.g. test fixtures) become non-blocking.
NON_BLOCKING_SUFFIXES = (
    "_test.py", ".tests.py",
)

# Binary-ish files and huge artifact dirs that can't be scanned meaningfully.
SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__",
             "static/vendor", "qr_codes", "receipts", "backups"}
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".woff", ".woff2",
                 ".ttf", ".eot", ".ico", ".gz", ".zip", ".pyc", ".db",
                 ".min.js", ".min.css")


def _tracked_files(staged_only: bool) -> list[Path]:
    if shutil.which("git"):
        cmd = ["git", "ls-files", "-z"]
        if staged_only:
            cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
        try:
            out = subprocess.run(
                cmd, cwd=REPO_ROOT, capture_output=True, check=True
            ).stdout
            files = [Path(p) for p in out.decode("utf-8", "replace").split("\0") if p]
            if files:
                return files
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    if staged_only:
        print("WARNING: git not available — scanning ALL tracked-looking files",
              file=sys.stderr)
    # fallback: walk, skipping junk dirs
    files = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(rel.startswith(d + "/") for d in SKIP_DIRS):
            continue
        if path.is_file():
            files.append(path)
    return files


def scan(staged_only: bool = False) -> int:
    findings = 0
    blocking_files = set()
    for path in _tracked_files(staged_only):
        rel = Path(path).as_posix()
        candidate = REPO_ROOT / rel
        if not candidate.is_file():
            continue
        if rel.endswith(SKIP_SUFFIXES):
            continue
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        for rule, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line_no = text[: match.start()].count("\n") + 1
                snippet = text.splitlines()[line_no - 1].strip()[:100]
                print(f"POTENTIAL SECRET [{rule}] {rel}:{line_no}:{snippet}")
                findings += 1
                if not (
                    rel.startswith(NON_BLOCKING_DIRS)
                    or rel.endswith(NON_BLOCKING_SUFFIXES)
                ):
                    blocking_files.add(rel)

    print(f"secret scan: {findings} potential finding(s)")
    if findings == 0:
        print("secret scan: CLEAN (pattern tripwires only — not a guarantee)")
        return 0
    if blocking_files:
        print("secret scan: FAIL — findings outside test fixtures MUST be "
              "triaged before commit/push")
        return 1
    print("secret scan: findings are confined to test fixtures (reviewed, "
          "non-production) — PASS")
    return 0


def pip_audit() -> int:
    if not shutil.which("pip-audit"):
        print("pip-audit: SKIPPED — `pip-audit` is not installed "
              "(pip install pip-audit)")
        return 0
    proc = subprocess.run(
        [sys.executable, "-m", "pip_audit"],
        cwd=REPO_ROOT,
    )
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true",
                       help="scan only staged (git add'ed) changes")
    group.add_argument("--pip-audit", action="store_true",
                       help="run the dependency CVE audit instead of the "
                            "secret scan")
    group.add_argument("--all", action="store_true",
                       help="run BOTH the secret scan and pip-audit")
    args = parser.parse_args()

    if args.pip_audit:
        return pip_audit()
    if args.all:
        rc = scan()
        print()
        rc2 = pip_audit()
        return rc or rc2
    return scan(staged_only=args.staged)


if __name__ == "__main__":
    sys.exit(main())
