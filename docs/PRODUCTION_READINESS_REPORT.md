# SkipLine — Production Readiness Verification Report

**Date:** Nov 5–6, 2026 · **Verifier session:** post-Phase-9 prodcheck
**Baseline commit (unchanged):** `b7006d90c0018a9b275e73edc18c2e59fea6a259` (Phase 9, pushed to `origin/main`)

---

## FINAL STATUS: VERIFICATION INCOMPLETE

Core engineering controls (MySQL migration chain **re-executed greenlet-free
Nov 6**, genuine CAS concurrency, AI provider transport hardening,
production secret enforcement **18/18 pytest + 20/20 matrix**) **all verified
passing**. Essential checks that could **not** be completed on this machine:

1. **Live production LLM integration** — no AI provider is configured or
   authorized (`AI_PROVIDER`, `AI_API_KEY` absent from `.env`). Only the
   transport layer was verified against a local mock.
2. **Mostly all greenlet-dependent execution** — from Nov 6, ~10:21, a
   Windows Code Integrity enterprise policy (ID `0283ac0f`) continues to
   block the venv's unsigned `_greenlet.cp312-win_arm64.pyd` (event 3077,
   re-confirmed 11:02). greenlet is a hard dependency of SQLAlchemy asyncio,
   so the **DB-backed part of the pytest suite** (~720 of 797 tests) and the
   remaining six **UI-acceptance flow replays** are **BLOCKED** on this
   workstation. Legitimate workarounds that needed no policy change were
   applied where they existed: `--noconftest` execution of DB-free tests
   (77/77 green, §1.6) and a sync `env.py` for the Alembic chain (**the
   migration revalidation is complete**, §1.1). The policy was not
   circumvented, weakened, or modified by the verifier; IT remediation is
   requested in `docs/IT_BLOCKER_CODEINTEGRITY_0283ac0f.md`, which carries
   the policy ID, event evidence, dependency chain, remediation options, and
   exact resume commands for an approved machine or CI runner.
3. Consequently **no re-run of the full suite exists after the in-place
   fixes** (§3: migration-0018 downgrade repair, placeholder blocklist,
   `.env.example`-marker helper); the standing evidence is the Phase-9
   baseline suite plus the targeted, greenlet-free re-verifications
   (§1.5–§1.7).

**Session-8 update (Nov 6, hosted CI):** once the CI workflow was
authorized and pushed, GitHub Actions executed the complete 857-test suite
on a hosted runner (real async fixtures; the greenlet block applies only to
this workstation): **857 passed, 0 failed, 715 warnings**, plus the MySQL
migration chain 0001→0018 up/down/re-up ALL-OK (§5 rows 36–37, runs
`42261999029` and `42262780689`). Automated pytest/migration verification
is now complete; the **six UI acceptance flows (§1.4)** and the
**live AI provider call (§1.2)** remain unverified, so the status stays
**VERIFICATION INCOMPLETE**. Production deployment is still **not
authorized**.

Production deployment is **not authorized** until the outstanding items in
§6 (and the §7 approved-environment checklist) are resolved.

**Session-9 addendum (Nov 8) — remaining-blocker matrix.** The full,
evidence-per-row matrix now lives in
`docs/PRODUCTION_ACCEPTANCE_REPORT.md` §10. Summary of what changed since
session 8: (a) local full suite re-run green — **857 tests / 0 failures /
0 errors / 4 Redis skips** (authoritative `full_junit.xml`; console line
truncated in the saved log), (b) app-side warning fixes landed and are
grep-verified at zero residual call sites, (c) the six-flow Playwright
harness was completed and every flow was blocked at collection by the
rearming greenlet policy block — per-flow code-level coverage is reviewed
in that §10.1 table, **no flow is claimed PASSED**, (d) backup/restore
executed live for the first time — `ALL-OK` exit 0 (run 9, §5 row 41) with
the banner-disclosed `sync-create_all` fallback mode; a real-alembic-chain
rehearsal remains queued, (e) warning analysis corrected to evidence-based
numbers (hosted 715 baseline; local grouped sum 678; residual breakdown
from log site-package paths), (f) Docker engine confirmed absent (CLI-only
machine), (g) SEC-009 human-review handover prepared
(`docs/SEC009_LEGAL_HANDOVER.md`; approval still outstanding).
**Verdict unchanged: B — VERIFICATION INCOMPLETE.** Ledger rows 38–42
below record each item's execution evidence.

---

## 1. Verification record by task

### 1.1 MySQL compatibility of migration 0018 — ✅ VERIFIED (Nov 5) · ✅ RE-EXECUTED (Nov 6, greenlet-free)

Executed against **disposable** MySQL database `skipline_phase9_prodcheck`
(MySQL server 8.0.46; the production `skipline` database was **never
connected to** — confirmed before and after via `SHOW DATABASES`).

| Step | Result | Duration |
|---|---|---|
| `alembic upgrade 0017` — full chain 0001→0017 (real migrations) | PASS | 10.9 s |
| Legacy-row preservation seeds (org/service/counter/token rows) | preserved at every step | — |
| `alembic upgrade head` → 0018 | PASS | 0.2 s |
| Downgrade `0018 → 0017` | PASS **after fix** (see §3.1) | 0.1 s |
| Re-upgrade → 0018 (idempotence) | PASS | 0.3 s |
| FK rule audit (`information_schema`) | all 14 FK actions match migration declarations (CASCADE/SET NULL as declared) | — |
| Disposable DB dropped afterwards | done | — |

SQLite-driven migration round-trip tests after the fix:
`tests/unit/test_migration_0018.py` + `test_migration_0017.py` +
`test_0015_schema_sync.py` → **4 passed** (Nov 5, pre-policy-block).

Environment: Windows ARM64, SQLAlchemy 2.0.30, alembic 1.13.1,
pymysql 1.1.1 (Alembic CLI) / aiomysql 0.2.0 (async app).

**Nov 6 re-execution (greenlet-free sync audit).** The chain was re-run on a
fresh disposable MySQL 8.0.46 database (`skipline_phase10_chain`, dropped
afterwards) using a temporary **sync** `env.py` — only the repo's env.py
needs the async engine/greenlet; the revision scripts themselves are pure
sync `op.*`. Results on the current code state:

| Step | Result | Duration |
|---|---|---|
| Full chain 0001→0017 | PASS | 15.7 s |
| Legacy rows seeded (2 tokens + org + service + counter) | present at every step | — |
| Upgrade head → 0018 | PASS | 0.3 s |
| FK audit (14 FKs) | all actions match declarations (CASCADE/SET NULL/RESTRICT as declared) | — |
| Named-index audit | **9/9** declared 0018 indexes present | — |
| Downgrade 0018→0017 | **PASS — no error 1553** (§3.1 fix re-verified live) | 0.1 s |
| Re-upgrade → 0018 | PASS, FK set identical, 9/9 indexes again | 0.3 s |
| Disposable DB dropped + temp env copy removed | done | — |

### 1.2 AI provider integration — ✅ TRANSPORT VERIFIED, ⛔ LIVE ENDPOINT UNVERIFIED

Configured provider: **none** (`.env` has no `AI_*` values) — the AI
assistant correctly degrades everywhere. Two verifications executed on Nov 5:

**(a) Safe mock-conversation suite:** `tests/api/test_ai_assistant.py` →
**20 / 20 passed** (2.06 s). Covered: provider errors, malformed protocol
responses, tool validation, confirmation gating, booking safety/idempotency,
manual↔AI continuity. Scripted mock LLM only — no external calls.

**(b) Real-HTTP transport check** (localhost mock OpenAI-compatible server,
dummy key `sk-dummy-…`, zero external network calls): **9 / 9 PASS** —
- OK reply returned verbatim,
- HTTP 500 → `AiProviderUnavailable` (degrades, never 500s the user),
- invalid JSON / missing `choices` / empty content → `AiProviderUnavailable`,
- slow server vs 2 s timeout → `AiProviderUnavailable` (with degrading log),
- `Authorization: Bearer …` header actually transmitted,
- model name transmitted,
- unconfigured provider raises before any outbound HTTP.
- **Dummy API key never appears in any log line** (type-only error logging).

**Not verified:** an actual production LLM endpoint (OpenAI or compatible).
Doing so requires an operator-provided, authorized `AI_PROVIDER/AI_BASE_URL/
AI_API_KEY/AI_MODEL` and explicit authorization for metered calls. Transport
behavior above is endpoint-agnostic (OpenAI-compatible contract).

### 1.3 Genuine concurrent-worker CAS verification — ✅ VERIFIED (Nov 5)

Executed on disposable MySQL DB `skipline_phase9_cas` with a **real
alembic-migration-built schema** (full chain to head) and **independent
aiomysql sessions/connections** per worker (stronger than the committed
in-memory SQLite simulation):

1. **Parallel race:** two tasks via `asyncio.gather`, independent sessions,
   both calling `workflow_service.advance_stage` on the same token →
   **exactly 1 winner**, the other raises `StageRaceError`
   ("Stage changed concurrently…" → HTTP 409 mapping), and **exactly one**
   `token_stage_logs` advance row. Never a silent overwrite.
2. **Sequential stale CAS:** session A captures its view; session B advances
   and commits; A's compare-and-swap update then matches **0 rows** →
   `StageRaceError` refused — no overwrite.

**2 / 2 passed** (12.1 s). Disposable DB dropped at teardown.

### 1.4 UI acceptance testing — ⚠️ PARTIALLY VERIFIED (details §2.1)

A 7-flow acceptance suite (HTTP-level replay of the exact requests the real
UIs issue) was executed once on Nov 5 12:30 **before** the policy block:

| Flow | Nov 5 result | Evidence |
|---|---|---|
| Kiosk guest booking + signed-ref receipt/QR + forgery/legacy-bypass denials | **PASSED** | booking OK; signature-less receipt → 404; `ref` receipt → PDF, `ref` QR → PNG; forged ref → 404; legacy `/receipts/…`, `/qr_codes/…` static paths → 404 |
| Dashboard/org workflow config HTML + JSON list | FAILED (test-fixture bug: Bearer header where the HTML view expects the `org_token` cookie) | fixture fixed post-run |
| User manual booking + owner-only tracking page | FAILED (same cookie-vs-header fixture bug; wrong history endpoint URL in test) | fixture fixed post-run |
| Employee stage progression (shift lifecycle → advance → mandatory-skip refusal → skippable skip with reason → recall → history audit) | FAILED (journey fixture broke on the auth fixup needed above) | depends on same fix |
| Walk-in booking carrying usable receipt ref | FAILED (same fixture) | depends on same fix |
| Manual→AI context handoff + AI confirmation gating + AI→manual prefill + booked-conversation terminality | FAILED (same fixture) | depends on same fix |

After fixing the test fixtures, **every endpoint/route used by the flows was
statically re-verified to exist with correct prefixes and auth** (workflow
router `/org/workflows*`, `/employee/tokens/{id}/workflow/{advance,skip,recall,
history,complete}`, `/employee/tokens/book-walkin`, tokens `/tokens/{id}/
{receipt,qr}`, AI router `/ai/*`, `/user/api/tokens/history`,
`/user/track/{id}` with `user_token` cookie auth) — but the re-run was then
**impossible** because of the greenlet policy block (§2.1).

Continuation-session (Nov 6) final status of the six mandated flows:

| Flow | Status this session | Reason |
|---|---|---|
| 1. Kiosk guest booking + signed receipt/QR + forgery denials | ✅ PASSED (Nov 5, live) | evidence above |
| 2. Dashboard/org workflow configuration (HTML + JSON) | ⛔ BLOCKED | Code Integrity policy (§2.1) — endpoint existence and auth statically verified instead |
| 3. User manual booking + owner-only tracking | ⛔ BLOCKED | same policy; endpoints statically verified |
| 4. Employee stage progression + recall + history | ⛔ BLOCKED | same policy; endpoints statically verified |
| 5. Walk-in booking with usable receipt ref | ⛔ BLOCKED | same policy; endpoints statically verified |
| 6. Manual↔AI continuity + confirmation gating | ⛔ BLOCKED | same policy; endpoints statically verified; mock-suite 20/20 stands |

Note: the underlying behaviors of the six un-re-run flows are covered by the
committed Phase-9 suites (e.g. `tests/api/test_workflows.py`,
`tests/api/test_ai_assistant.py`, KYC/shift/receipt flows), which passed at
the Phase-9 baseline. The prodcheck replay adds end-to-end "one continuous
journey" coverage that could not be completed in this session.

### 1.5 Deployment configuration review — ✅ EXECUTED (static + 5-case→20-case executed enforcement matrix)

Executable enforcement checks (Nov 5–6; pure settings load — no DB/network;
controlled `.env` files, exactly as the app does at startup):

| Case | Expected | Result |
|---|---|---|
| `APP_ENV=production` + `change-me…` placeholder secrets (as observed verbatim in the local dev `.env`) | refuse to start | **PASS (after fix §3.2)** — was FAIL pre-fix |
| `APP_ENV=production` + missing `DB_PASSWORD` | refuse to start | PASS — raises at startup |
| `APP_ENV=production` + strong values | starts cleanly | PASS, no warnings |
| `APP_ENV=development` + placeholders | warn only | PASS — 2 warnings, no substitution |
| `APP_ENV=production` + **verbatim copy of the committed `.env.example`** | refuse to start | **PASS (after marker fix §3.2, Nov 6)** — was FAIL pre-fix: the `<set-a-long-random-64+-char-secret>` markers (35 chars) passed the ≥32 length check and the app constructed |

**5 enforcement behaviors ALL-OK** (the marker fix caused no regression:
the original 4-case suite re-ran ALL-OK post-fix; both edited files pass
`py_compile`).

**Completion session (Nov 6): the full matrix** (`config_matrix.py`,
standalone — pytest itself is blocked on this machine, §1.6) covers all
mandated cases plus the helper-safety lattice: dev/dev-default placeholders
rejected on BOTH `SECRET_KEY` and `JWT_SECRET_KEY` independently, unfilled
SECRET/JWT/ADMIN markers rejected, dev warns ×2 without substitution,
ephemeral random on empty secret, DB password warn-only in dev /
required+raising in prod / angle-brackets accepted, helper malformed-safe —
**20/20 ALL-OK**.

**Session-4 execution (Nov 6, later).** The pytest file itself was
successfully executed on this machine with the standard `--noconftest` flag
(legitimately skipping only `tests/conftest.py`'s blocked session-scoped DB
fixture — these tests use no DB): **18/18 items passed**
(`pytest tests/test_config_security.py -v --noconftest`). One honest
correction was made first: the internal-angle-brackets case had been
authored with a 28-char string while claiming 32 — production **correctly**
rejected it as too short; the test string was fixed to a true 32-char value
and the same case was added to the standalone matrix (hence 20/20). No
validator change was needed; the validation behaved correctly throughout.
The durable regression file `tests/test_config_security.py` (18 pytest
items; several cover multiple values) stays in the tree and runs under
plain pytest on any approved environment.

Static findings (files reviewed: `.env.example`, current dev `.env` (without
printing values), `docker-compose.yml`, `Dockerfile` target usage,
`nginx/nginx.conf`, `nginx/nginx.production.conf`, `app/config.py`,
`app/main.py`, `requirements.txt`, `docs/deployment.md`, `docs/database.md`):

- ✅ `.env` is gitignored; `.env.example` is comprehensive (incl. AI provider,
  ClamAV, CSRF, Redis scaling, nginx selection, secret generation commands).
- ✅ Compose requires `MYSQL_ROOT_PASSWORD` and `DB_PASSWORD` explicitly
  (`:?` guards); app service pulls `.env`; Redis/ClamAV health-gated.
- ✅ Secret production enforcement is a **pydantic `model_validator`** and
  genuinely runs at settings construction (proven by the executed cases).
- ✅ nginx production conf: TLSv1.2/1.3 only, HSTS `always`, certbot webroot,
  per-zone rate limits, security headers owned by the app (no duplication);
  dev conf serves HTTP with headers and a commented-out 301 (dev only).
- ✅ FastAPI: `docs_url`/`redoc_url` disabled in production; session cookies
  `https_only` in production; CSRF enforcement automatic in production;
  ClamAV opt-in records honest `scan_status="disabled"` (never "clean").
- ✅ `requirements.txt` fully pinned; backup via compose profile + script.
- ⚠️ **Logging:** loguru sinks to **stderr only** — no file sink, no rotation.
  The compose file mounts an `app_logs` volume that **nothing writes to**.
  Persistent/rotated logs depend on the container runtime's collection.
  *Recommend:* file sink (or explicit stdout-log collector) before launch.
- ⚠️ Compose pins `mysql:8.3` while the live verification server was
  **8.0.46** (both MySQL 8.x; the verified chain is 8.0-compatible). Keep
  major version awareness on production servers.
- ⚠️ The current dev `.env` is entirely development-grade (`APP_ENV=
  development`, `APP_DEBUG=true`, placeholder secrets/payments/mail, no
  Redis, no AI provider). It is correctly blocked from production use by the
  post-fix validators, but a real production `.env` must be authored fresh.
- ℹ️ Auth rate limiting is layer-consistent (nginx `5r/m` auth zone + app-level
  20/60 s auth-path limit, SEC-004).
- ⚠️ **Backup/restore scripts have never been executed against a live MySQL**
  (honest status per `docs/OPEN_DECISIONS.md`); `scripts/backup.sh` itself
  reviewed sound (gzip + round-trip + sha256 + min-size guard + retention),
  `scripts/restore.sh` present — the first real execution belongs to
  deployment (§5 row 27).
- ⚠️ **Legal/compliance review (SEC-009)** identified as an outstanding
  human obligation; no fabricated sign-off recorded.
- MySQL version consistency: compose pins `mysql:8.3`; verification server
  was 8.0.46; `docs/` MySQL-version references consistent (both 8.x; see
  ledger row for the reconciliation reminder).

### 1.6 Full-suite re-run — ⛔ BLOCKED locally (Nov 6) · ✅ EXECUTED on hosted CI (Nov 6)

**Hosted resolution (later the same day):** after the CI workflow was
authorized, committed and pushed, GitHub Actions executed the complete suite
on `ubuntu-latest` (Python 3.12, real async fixtures with greenlet — the
module this workstation's Code Integrity policy blocks): **857 passed,
0 failed, 0 errors, 0 skipped** (with the MySQL migration chain 0001→0018
up/down/re-up ALL-OK, 14/14 declared FKs, 9/9 indexes). See §5 ledger rows
36–37 for the full run chain (runs #1–#4 failed honestly during
diagnostics; #5 and #6 are green). UI acceptance (§1.4) and the AI live
provider (§1.2) remain unverified, so the overall status is unchanged.

After the local policy change, `pytest` in-memory-SQLite setup fails at
`greenlet_spawn` import. The full 779/775 suite (and any DB-backed test) can
only be re-run once the workstation permits the module (signing/policy
exemption) or on an allowed machine. Targeted verifications that ran:

- Nov 5 (pre-block): full MySQL chain, CAS 2/2, AI 20/20 + 9/9 transport,
  migration unit tests 4/4, kiosk acceptance flow.
- Nov 6 (no greenlet needed): config enforcement 4/4 (after fix) + verbatim
  `.env.example` case (after marker fix), endpoint existence cross-check,
  `.env`/gitignore status.
- Nov 6 (continuation session): greenlet block re-confirmed still in force;
  both in-place fixes re-inspected; verbatim `.env.example` defect discovered,
  corrected, and re-verified.
- Nov 6 (session 4): **partially restored execution via `--noconftest`** —
  the standard pytest flag legitimately skips `tests/conftest.py` (and its
  blocked session-scoped DB fixture) for tests that use no DB. Executed
  greenlet-free on this workstation: `tests/test_config_security.py`
  **18/18 PASS** (§1.5) and five DB-free unit files
  (`test_security` 8, `test_signed_qr` 15, `test_pdf_generator` 6,
  `test_phase8_security` 19, `test_redis_bus` 11) = **59 PASS**, total
  **77 executed, 0 failed**. The remaining ~720 of the 797 collected tests
  are still blocked at the fixture/greenlet wall — these counts do not
  replace the full-suite run. Note: the committed
  `tests/unit/test_config_security.py` contains DB-seeded cases and runs
  only on an approved environment.

### 1.7 Completion session (Nov 6, continuation) — summary of results

- **Config security matrix 20/20 ALL-OK** (`C:\Users\ASUS\AppData\Local\
  Temp\opencode\prodcheck\config_matrix.py`; synthetic values only, nothing
  sensitive printed). `_is_unfilled_marker()` refined to an exact
  full-marker rule — see §3.2. Durable regression file
  `tests/test_config_security.py` added (18 pytest items, hermetic) —
  **executed 18/18 PASS** under `pytest --noconftest` on this machine later
  the same day (§1.5).
- **Migration chain re-ran greenlet-free (session 4, §1.1):** the alembic
  chain was re-executed on a fresh disposable MySQL 8.0.46 DB via a
  temporary sync `env.py` (the revision scripts are pure sync) — up/down/
  re-up ALL-OK, 14 FK actions verified, 9/9 named indexes, legacy rows
  preserved, no error 1553. What remains blocked for alembic is only the
  repo's async-env CLI command (`env.py` needs greenlet); the verification
  itself is complete.
- **Full suite: 797 tests collected, 0 collection errors; execution BLOCKED**
  at the session-scoped autouse DB fixture in `tests/conftest.py` (greenlet).
  No greenlet-free full-suite substitute exists because the fixture is
  autouse for every test.
- **Six UI acceptance flows: BLOCKED** (table in §1.4).
- **AI live provider: NOT VERIFIED retained** — still no `AI_*` configured
  or authorized; mock-transport evidence (9/9 + 20/20) stands.
- **IT remediation report created:**
  `docs/IT_BLOCKER_CODEINTEGRITY_0283ac0f.md` — policy ID, event-3077
  evidence, dependency chain, authorized remediation options, and exact
  resume commands for an approved machine or CI runner. No policy was
  bypassed, weakened, or modified; no sign/replace of the blocked binary.
- **No CI runners exist** (`.github/workflows` absent) → no authorized
  environment available from this checkout.
- **Master-requirements traceability confirmed in implementation** (§9):
  exactly-four-Super-Admin cap and §4.4 dual approval, both with committed
  tests.

- **Session-4 addendum (later Nov 6):** policy re-confirmed (event 3077 at
  ~11:02) with still no approved environment/CI runner; the security
  regression file **executed 18/18 PASS** under `pytest --noconftest` plus
  **59 DB-free unit tests PASS** (77 total, §1.6); the **MySQL migration
  chain was re-executed greenlet-free** on a fresh disposable 8.0.46 DB —
  up/down/re-up, 14 FKs, 9/9 indexes, legacy rows, **no error 1553**
  (§1.1). Six UI flows and the DB-backed suite remainder remain blocked.
  Live LLM still NOT VERIFIED.

---

## 2. Environment incidents discovered during verification

### 2.1 Windows Code Integrity policy blocks unsigned native modules (Nov 6, ~10:21)

Evidence: `Get-WinEvent Microsoft-Windows-CodeIntegrity/Operational` event 3077:
`python.exe attempted to load …venv\Lib\site-packages\greenlet\
_greenlet.cp312-win_arm64.pyd that did not meet the Enterprise signing level
requirements or violated code integrity policy (Policy ID:{0283ac0f-…})`.
The files are unchanged since their June-2026 install — this is a **machine
policy refresh**, not file tampering. Consequence: all async-DB execution
(SQLAlchemy asyncio, aiomysql, pytest in-memory fixtures, the app's own DB
layer) is blocked on this workstation. All passing evidence used here was
produced before enforcement began. **Remediation:** have the workstation's
policy owner sign the file or add a policy exemption/window, or run the
remaining UI-acceptance re-verification on a machine under an eligible
policy — the formal request with exact resume commands is
`docs/IT_BLOCKER_CODEINTEGRITY_0283ac0f.md`. Do **not** patch/replace the
binary to evade the policy (the verifier did not).

### 2.2 Credential-echo incident (disclosure)

During the Nov 5 session, one verification command inadvertently echoed the
raw `.env` key list (including local dev values for `DB_PASSWORD` and
`ADMIN_PASSWORD`) into the local session transcript. Values are for a
localhost development MySQL; impact is machine-local, **but if this
transcript is ever shared outside the trusted team, rotate the local dev
MySQL `root` password and the dev `ADMIN_PASSWORD`** — treat the current dev
DB as purge-and-recreate. No production credentials were involved (none
exist in `.env`).

---

## 3. In-place fixes and additions (the working-tree changes — intentionally uncommitted)

Base repo state at session start: clean at `b7006d90…`. Nothing was committed
or pushed during verification. Exactly two tracked files are modified
(corrections below), plus untracked additions: `tests/test_config_security.py`
(new regression tests, §3.2), this report, and
`docs/IT_BLOCKER_CODEINTEGRITY_0283ac0f.md`.

### 3.1 `migrations/versions/0018_workflows_and_ai.py` — MySQL-1553 downgrade repair

The committed `downgrade()` dropped FK-backed **indexes before** dropping
their argument tables. MySQL 8 refuses to drop an index that still backs a
declared FK (error 1553); it passed on SQLite only because SQLite does not
enforce this. Discovered live on the disposable DB during §1.1. Fix: drop the
five tables first (their indexes and FK-enforcing indexes go with them).
Verification: full MySQL up/down/re-up chain (§1.1) and the SQLite round-trip
migration tests **4 passed**. Continuation note (Nov 6): the fix comment no
longer references removed standalone index-drop code (comment-accuracy only,
no behavior change; both files `py_compile`-clean). The `_has_index` helper
is now unused (kept; removal is left for the eventual commit review).

### 3.2 `app/config.py` — production placeholder-secret enforcement gaps (two findings)

**Finding A (Nov 5).** The `change-me-to-a-64-char-random-string` /
`change-me-to-another-64-char-random-string` placeholders — observed verbatim
in the local development `.env` (they were never in any committed
`.env.example` revision) — were **not** in `_INSECURE_SECRET_VALUES` (and
pass the ≥32 length check), so `APP_ENV=production` with those values would
have started the app on known-public signing keys. Executed proof pre-fix:
the settings load constructed successfully; post-fix it raises at startup.
Fix: added the exact strings (plus `"change-me"`) to the frozenset.

**Finding B (Nov 6, continuation session — fresh evidence).** The committed
`.env.example` itself uses `<set-a-long-random-64+-char-secret>` markers. A
**verbatim copy of `.env.example` with `APP_ENV=production` constructed
successfully** pre-fix (executed probe; 35-char markers pass the ≥32 length
check and are not blocklisted) — i.e. the original defect class was still
open for the documented example values. Smallest safe correction: a shared
`_is_unfilled_marker()` helper rejects a `SECRET_KEY`/`JWT_SECRET_KEY`/
`ADMIN_PASSWORD` value in its **entirety** when it is an unfilled
`.env.example` template marker (`starts with` `<`, `ends with` `>`; Nov 6
session-3 refinement — earlier the rule matched any value *containing*
`<`/`>` anywhere, which would have over-rejected legitimate secrets with
internal angle brackets). Malformed/truncated fragments are handled safely
(a fragment is not a full marker; truncated marker strings are still
rejected in production by the ≥32 minimum-length rule; known placeholder
strings remain covered exactly by `_INSECURE_SECRET_VALUES`). The rule is
deliberately **not** applied to `DB_PASSWORD` (a legitimate password may
contain `<`/`>`; a wrong DB password already fails loudly at first
connection, and compose hard-guards the MySQL root password).

**Re-verification (both Nov 6):**
- verbatim `.env.example` → production: **rejected** (`ValidationError`,
  SECRET_KEY placeholder message) — was CONSTRUCTED pre-fix;
- original 4-case enforcement suite: still **4/4 ALL-OK** (marker fix caused
  no regression);
- full matrix (sessions 3–4): **20/20 ALL-OK**, including acceptance of
  strong secrets and of a true 32+-char secret with internal `<`/`>` (one
  initial pytest case was authored with a 28-char string — production
  correctly rejected it; the test string was corrected, no validator
  change needed, §1.5);
- regression file `tests/test_config_security.py` added — 18 pytest items,
  hermetic via `_env_file=None`; **executed on this machine under
  `pytest --noconftest`: 18/18 PASS** (the flag skirted only conftest's
  blocked DB fixture, §1.6), and the standalone equivalent matrix was
  executed greenlet-free;
- both edited files pass `py_compile`.

Both diffs are shown verbatim in the session record; `git status` shows only
these two modified files.

---

## 4. Verified-secure behaviors carried forward from Phase 9 (not re-run)

The committed Phase-9 audit (§ `docs/PHASE_9_FINAL_ACCEPTANCE_AUDIT.md`)
already established, and this session did not contradict: confirmation-gated
booking (no booking without explicit user confirmation), booked-conversation
terminality, idempotent booking keys, signed receipt/QR refs with purpose
binding (re-verified the kiosk denials live, §1.4), HMAC-based drift-free
auth, scoped RBAC, and the LLM never possessing booking authority.

---

## 5. Overall test ledger (exhaustive, as executed)

| # | Verification | When | Result | Environment |
|---|---|---|---|---|
| 1 | Alembic chain 0001→0017 on disposable MySQL | Nov 5 | PASS (10.9 s) | MySQL 8.0.46, disposable DB only |
| 2 | 0018 upgrade / downgrade / re-upgrade + FK audit + legacy-row survival | Nov 5 | PASS (0.2/0.1/0.3 s) — downgrade only **after fix** | same |
| 3 | SQLite migration round-trip unit tests (0015/0017/0018) | Nov 5 | 4 passed | in-memory SQLite |
| 4 | Genuine CAS: parallel + stale sequential | Nov 5 | 2 passed | disposable MySQL, independent aiomysql sessions |
| 5 | AI mock suite `test_ai_assistant.py` | Nov 5 | 20 passed | SQLite fixture, mock LLM |
| 6 | AI real-HTTP transport (mock server) | Nov 5 | 9/9 PASS | localhost only, dummy key |
| 7 | UI acceptance suite (7 flows) | Nov 5 | 1 passed / 6 fixture-buggy | SQLite fixture (pre-block) |
| 8 | UI acceptance fixed re-run | Nov 6 | **BLOCKED** by Code Integrity policy | — |
| 9 | Config enforcement (4 cases) | Nov 6 | 4/4 ALL-OK (after fix) | no DB/network |
| 10 | Endpoint existence cross-check for all UI flows | Nov 6 | PASS (static) | — |
| 11 | Full 779/775 suite re-run | Nov 6 | **BLOCKED** | greenlet policy |
| 12 | Docker-compose live bring-up | not run | Docker daemon down on this machine | — |
| 13 | Verbatim committed `.env.example` copy in production (pre-fix probe) | Nov 6 | **CONSTRUCTED (defect confirmed)** | no DB/network |
| 14 | Same case after `.env.example`-marker fix (`_is_unfilled_marker`) | Nov 6 | rejected — PASS | no DB/network |
| 15 | `ADMIN_PASSWORD` unfilled-marker (`<set-a-strong-password-12+-chars>`) in production | Nov 6 | rejected — PASS | no DB/network |
| 16 | Regression: original 4-case suite re-run after marker fix | Nov 6 | 4/4 ALL-OK | no DB/network |
| 17 | `py_compile` of both in-place fix files | Nov 6 | OK | — |
| 18 | Git-state audit of continuation session | Nov 6 | 2 modified + 1 new doc; no staged/untracked drift | — |
| 19 | Greenlet policy-block re-confirmation | Nov 6 | still blocked (documented) | workstation |
| 20 | Full config-security matrix (incl. helper lattice + 32-char internal-bracket case) | Nov 6 (sessions 3–4) | **20/20 ALL-OK** | no DB/network; greenlet-free |
| 21 | Alembic chain re-run via repo async `env.py` | Nov 6 (session 3) | **BLOCKED** — `migrations/env.py` needs greenlet | workstation |
| 22 | Full-suite collection (`pytest --co`) | Nov 6 (session 4, re-confirmed) | **797 collected, 0 collection errors** | imports are greenlet-free |
| 23 | Full-suite execution | Nov 6 (sessions 3–4) | **BLOCKED** — session-scoped autouse DB fixture (greenlet) for DB-backed tests | greenlet policy |
| 24 | Regression file `tests/test_config_security.py` added + hermeticity fix (`_env_file=None`) | Nov 6 (session 3) | 18 pytest items in tree; executed same day (row 29) | — |
| 25 | Master-requirements traceability (exactly 4 Super Admins; §4.4 dual approval) | Nov 6 (session 3) | implementations + committed tests located — see §9 | static |
| 26 | IT remediation report for policy `0283ac0f` | Nov 6 (session 3) | created + updated session 4 (`docs/IT_BLOCKER_CODEINTEGRITY_0283ac0f.md`) | — |
| 27 | Backup/restore rehearsal against live MySQL · Docker compose bring-up | not run | backup/restore scripts never executed live per OPEN_DECISIONS; Docker daemon down on this machine | — |
| 28 | CI-runner availability check | Nov 6 | none — `.github/workflows` absent | repo |
| 29 | **Regression file under `pytest --noconftest`** | Nov 6 (session 4) | **18/18 items PASS** (one case's 28-char string corrected to true 32 — validator was right) | this machine, no DB |
| 30 | **MySQL chain re-executed greenlet-free** (sync env.py, disposable `skipline_phase10_chain`) | Nov 6 (session 4) | **ALL-OK**: 0001→0017 15.7 s; head 0018 + FK 14/14 + 9/9 indexes; **downgrade no-1553 PASS**; re-upgrade idempotent; legacy rows preserved; DB dropped | MySQL 8.0.46, disposable DB only |
| 31 | DB-free unit subset (`test_security`, `test_signed_qr`, `test_pdf_generator`, `test_phase8_security`, `test_redis_bus`) | Nov 6 (session 4) | **59/59 PASS** (`--noconftest`) | this machine, no DB |
| 32 | CodeIntegrity event 3077 re-confirmation | Nov 6 (session 4) | still firing (latest 11:02 local) | workstation |
| 33 | **CI environment prepared** (workflow + guide + CI migration script; `aiosqlite` dependency fix in `requirements.txt`) | Nov 6 (session 5) | `.github/workflows/production-readiness-tests.yml` + `docs/CI_VERIFICATION_GUIDE.md` + `scripts/ci_migration_chain_check.py`; YAML statically validated (structure, explicit push/PR/workflow_dispatch events, minimal `contents: read` permissions, health-gated disposable services, no secrets referenced); **no hosted run executed — awaiting explicit user activation**; this changes no test results | static/local |
| 34 | **CI pre-activation review** — corrections applied before first activation (no behavior marked passed) | Nov 6 (session 6) | Workflow: quoted `"on":` key (YAML 1.1 parsers coerced bare `on` to boolean `True`) and `set -o pipefail` + `tee` for pytest (a failed run's exit code was previously masked by the trailing echo) — re-validated with `check-jsonschema` against GitHub's official `github-workflow.json` schema → "ok". Migration script: prefix-based DB gate replaced with an **exact allowlist** (`ALLOWED_MIGRATION_DB = "skipline_ci_migrate"`; production-like/empty/all other names refused before any connection), host restricted to loopback, `DB_PORT` validated 1–65535, non-empty synthetic user/password required, and every `DROP` routed through `drop_disposable_check()` inside a `try/finally` teardown so no exception path can drop an unrelated DB; module made side-effect-free for hermetic import. New gate tests `tests/test_ci_migration_gate.py`: **53/53 passed** (`pytest --noconftest`, synthetic values only). Local smoke: non-allowlisted name refused pre-connection; valid config → controlled greenlet failure (expected on this workstation), disposable DB dropped, exit 1, password/URL redacted in log, **no `skipline_ci*` DB remained on the server**. Dependency check: `aiosqlite==0.22.1` + `greenlet==3.5.2` pins consistent with SQLAlchemy 2.0.30 / Python 3.12. **This is preparation-review evidence only — the ~720 DB-backed tests remain BLOCKED and the full suite is NOT passed; hosted CI still requires explicit user activation.** | static/local |
| 35 | **Final CI destructive-safety review** (pre-activation, read-only + genuine-defect fix) | Nov 6 (session 7) | Found & fixed a true defect in `scripts/ci_migration_chain_check.py`: setup previously executed an unconditional `DROP DATABASE IF EXISTS skipline_ci_migrate` before creation — a pre-existing DB from any other source would have been destroyed. Now: `refuse_if_preexisting()` reads `SHOW DATABASES` and **refuses + preserves** any pre-existing DB (no DDL issued); a `created_this_run` ownership flag plus `teardown_allowed()`/`perform_drop()` gate **every** teardown — only the exact allowlist name, only if this run created it. Proven end-to-end on disposable MySQL 8.0.46: pre-seeded `skipline_ci_migrate` with a marker table → script refused (exit 1) → DB **still existed, marker intact** afterward. Normal-flow smoke re-verified (create → expected greenlet failure → owned-DB dropped → redacted log → no leak). Hermetic gate tests extended: **60/60 passed** (`pytest tests/test_ci_migration_gate.py -v --noconftest`; includes new ownership/preservation cases: refuse-if-preexisting, owned-vs-unowned teardown, exact-DDL recording, guard-fires-before-engine-creation). Workflow comments aligned with the hardened gate; `check-jsonschema` re-run → "ok"; `actionlint` not installed (documented limitation; not downloaded — Application-Control risk). No hosted run triggered; **full suite still NOT passed; status remains VERIFICATION INCOMPLETE.** | static/local |
| 36 | **Authorized CI commit + push + hosted runs #1–#4 (DIAGNOSTIC, honest failures)** | Nov 6 (session 8) | Commit `c0a30e59f9…` "ci: add hardened production readiness verification" (exactly the 10 reviewed paths, 2296 insertions) pushed to `origin/main` after the 78 hermetic tests passed and a secret scan was clean; remote was at expected `b7006d9` (no force push). Hosted evidence channel: GitHub's job-log/artifact APIs require admin tokens (none available in this environment), so failures were surfaced via `::error::` check-run annotations (commits `ada357f`, `b556d30` — diagnostics only, no test changes) read through the public check-runs API. Runs: #1 `42247780525` (c0a30e5) both jobs FAILED (pytest exit 4: `import razorpay` → `pkg_resources` missing on Python 3.12 — razorpay 1.4.2 imports `pkg_resources` but declares only `requests`, and py3.12 venvs no longer bundle setuptools; migration exit 1). #2/#3 `42249367084`/`42250169820`: root causes confirmed. Fixes pushed: `62abd35` (`setuptools==75.8.0` pin in `requirements.txt` — PyPI-verified, still ships pkg_resources; run #4 `42251050860` then ran **855 passed / 2 failed** real results) and `df8b712`: (a) `test_prod_missing_db_password_raises` now deletes the ambient `DB_PASSWORD` OS env var CI exports (pydantic reads OS env even with `_env_file=None`; sibling dev-mode test already did this) — verified locally with `DB_PASSWORD` exported; (b) `test_notify_token_created_dispatches_email` explicitly configures the mail provider under monkeypatch (CI has no `.env`; `provider_ready=False` → outbox row was `SUPPRESSED` not `PENDING`; same pattern as the neighboring SMTP-configured test) — 10/10 local; (c) FK audit: 0018's declared 14 FKs collapse to at most 9 distinct `(table, delete_rule)` pairs — the old `>= 10` threshold was unreachable **by construction** and CI's complaint of exactly 9 was correct; replaced with an **exact equality audit against the 14 declared FKs** (constraint name + table + referenced table + delete rule) — strictly stronger than the count. 78 hermetic tests re-passed; secret scan clean; `check-jsonschema` "ok". | hosted GitHub Actions (`ubuntu-latest`, MySQL 8.0.46 / Redis 7 service containers) + local |
| 37 | **Hosted verification GREEN (runs #5 and #6) — full suite executed on a hosted runner** | Nov 6 (session 8) | Run #5 `42261999029` (commit `df8b712`): **both jobs SUCCESS** — `pytest-suite` and `mysql-migration-chain`. Run #6 `42262780689` (commit `b628077`, which additionally records the success-path tally/verdict as `::notice::` annotations readable auth-free). Exact tallies captured from the public check-run annotations API: **pytest: 857 passed, 0 failed, 0 errors, 0 skipped, 715 warnings in 52.99 s** (fresh Python 3.12, real async fixture path with greenlet — the capability this workstation lacks); **migration chain: ALL-OK — 0001→0018 up/down/re-up verified, all 14 declared FKs and 9/9 declared indexes match the migration, legacy rows preserved at every step, no MySQL error 1553**, run duration ≈ 52 s. URLs: `https://github.com/Arunkumar-codec/skip-line/actions/runs/42261999029` and `…/42262780689`. Commit integrity: 6 commits pushed non-force, working tree clean after each. **Status remains VERIFICATION INCOMPLETE** — the six UI acceptance flows (§1.4) and the AI live-provider call (§1.2) remain unverified; this result covers the automated pytest + migration-scope production-readiness checks only. | hosted GitHub Actions (`ubuntu-latest`) |
| 38 | **Full-suite local re-run + app-side warning fixes + deployment config hardening** (closure phase) | Nov 7–8 | **Full 857-collected suite passed locally exit 0** (853 passed + 4 real-Redis integration `ssss` skips; ran during a greenlet-policy-open window) — evidence `%TEMP%\opencode\prodcheck\full_run.log` + `full_junit.xml`. Warning reductions applied in-repo: 7× `model_config = ConfigDict(from_attributes=True)` in `app/schemas/finance.py`; 57 legacy `TemplateResponse(name,…)` → `TemplateResponse(request,…)` across `app/routers/views_*` + `org.py` + `public.py` (79 views tests re-passed); `ai_assistant_service.py` `datetime.now(timezone.utc)`. New production hard-gate: `APP_DEBUG=true` + `APP_ENV=production` now raises (`app/config.py` model validator) — `tests/test_config_security.py` → **19/19 passed** incl. new `test_prod_rejects_debug_true`. Compose: `mysql:8.0.46` pinned (hosted-verified engine), `app_logs` volume used via new `LOG_DIR`/`LOG_FILE_LEVEL` + loguru rotating file sink (20 MB × 5) in `app/main.py` lifespan; `.env.example` documents both. Remaining warnings are third-party (python-jose ≈ 88%, razorpay `pkg_resources`, reportlab) — categorized in `docs/PRODUCTION_ACCEPTANCE_REPORT.md` §6, none suppressed. | local venv, greenlet-open window + static |
| 39 | **AI provider failure-path durability suite** | Nov 7 | `tests/unit/test_ai_provider_resilience.py` — **6/6 passed**: upstream timeout, HTTP 429, malformed JSON, empty action payload, unusable response that cannot inject `confirmed` (booking only via `/ai/conversations/{id}/book`), API-key-never-logged assertion, deterministic error replies. Harness: real `httpx.AsyncClient` captured BEFORE monkeypatching `ai_provider.httpx` + `httpx.MockTransport`; `scripts/mock_ai_provider.py` = deterministic OpenAI-compatible strict-JSON action provider (data-only `TOOL_RESULT` parsing; directives `org/service/search/status`). Live-LLM call still requires authorization (blocker 1) | local, no network |
| 40 | **Six-flow browser acceptance harness built; flows BLOCKED by workstation policy** | Nov 7–8 | Playwright 1.64.1 + Chromium installed (exit 0, `winldd` check). Full harness: `tests/acceptance/conftest.py` (disposable `skipline_acceptance_<stamp>_<hex>` DB, refuse-if-exists, alembic subprocess with real `.env` DB creds, uvicorn + mock-AI subprocesses on free ports, **teardown safety net drops the DB on every setup-failure path**) + six flow files mapped to real routes/selectors (auth logins, `#wfService`, `#bookModal`, `#tokenNum`, employee shift/stage ops, kiosk, AI chat/`#confirmBar`/`#draftBody`/`#manualPrefill`, `prompt()` dialog recall). All files `py_compile` OK. **Execution BLOCKED**: `pytest` collection fails importing `playwright` → `greenlet` DLL blocked by App Control (`0283ac0f`) - NO bypass attempted; all 6 flows recorded BLOCKED, evidence `acc_flow1e.log`. Harness-level bugs (mobile digits, teardown wrap, numeric stamp) fixed and re-verified; scratch DB inventory verified **zero orphans** after each killed run | local; browser layer policy-blocked |
| 41 | **Backup/restore rehearsal — FIRST live execution, ALL-OK (exit 0)** | Nov 8 | New `scripts/backup_restore_rehearsal.py`: real `mysqldump` (backup.sh flags: `--single-transaction --routines --events --triggers --set-gtid-purged=OFF`) → gzip w/ sha256 + min-size check → restore via `mysql` client into a fresh disposable DB → schema/rows/FK/`alembic_version` parity + application-level read → owned-only teardown (DROP just its own `skipline_bakrehearsal_20261108100159` + `…bakrestore_…`). Runs 1–8 hardened the script itself (dispose-before-drop, staged-connection leaks fixed via `with`-blocks, `backups_dir` NameError) — round 9 (`%TEMP%\opencode\prodcheck\rehearsal_run9.log`): **ALL-OK in 32.1 s** — artifact 114 725 B unpacked SQL, `sha256=4383d41f…ac0278`, restore parity tables=65, `alembic_version=0018`, FKs=134, `RH-0001@Rehearsal Clinic` + 2 tokens, users=1, stages=(registration,1)(consultation,2), waiting tokens=2 read from restored DB. **Disclosure:** source schema was built in the banner-disclosed `sync-create_all` mode because the workstation policy again blocked alembic's async stack (greenlet) — the rehearsal's backup/restore mechanics (the requirement §27 targets) are fully verified; a repeat run via the REAL migration chain remains queued for the policy-open window. Protected DBs (`skipline`, `skipline_phase9_prodcheck`, `skipline_synccheck`) printed untouched every run; post-run orphan check: none | MySQL 8.0.46 localhost, disposable DBs only |
| 42 | **Full secret scan with per-finding classification** | Nov 8 | Two-grammar scan (`KEY=value` + YAML `KEY: value` credential-key names, URL-embedded-credential, provider-key formats incl. PEM/AKIA/sk-/rzp_/AIza/ghp_/xox-/AC+32hex) over 2106 text files + each of the 3464 entries of an 11.4 MB zip. **Every** non-clean match individually classified in `docs/PRODUCTION_ACCEPTANCE_REPORT.md` §9: real local credentials live ONLY in git-ignored `.env` (MySQL root password, dev admin password; `git log --all -- .env` = never committed); all third-party service values there are placeholders (`change-me-…`, `your-…`, dummy `ACxxxxxx…`, `rzp_test_…`); CI workflow contains only `ci-only-synthetic-password`; compose is `${VAR:?required}` (no defaults slip); `backups/*.sql.gz` decompressed = **0** credential matches; zip = vendored third-party libraries (cryptography/pip fixtures), no project secrets; ~146 colon-style hits = ordinary code identifiers. **No finding suppressed**; residual risks recorded (strong-but-synthetic constants pass strength validators by design → operator `.env` procedure is the control; full git-content history scan not performed) | this workstation |

Resources: disposable DBs `skipline_phase9_prodcheck` (dropped) and
`skipline_phase9_cas` (auto-dropped); real DB `skipline` and pre-existing
`skipline_synccheck` untouched. No paid calls, no real bookings/payments, no
secrets written to logs. **Session-7 addendum:** the final destructive-safety
leak check found `skipline_phase9_prodcheck` STILL PRESENT on the server
(despite the "(dropped)" note above — earlier-session cleanup apparently was
not executed/failed). This review did NOT drop it — destructive operations
against databases outside the current session's own artifacts are out of
scope; the operator should verify and remove it. All `skipline_ci*` artifacts
created across sessions were verified dropped/absent.

---

## 6. Outstanding blockers (ranked)

1. **Authorization + configuration of a real LLM provider** (AI_* in env;
   funded account; written authorization for metered test calls) then a live
   short-call check of the same 8 transport behaviors plus one
   confirm-refused booking round-trip. Everything short of this is verified.
2. **Code Integrity policy `0283ac0f` remediation** (IT:
   `docs/IT_BLOCKER_CODEINTEGRITY_0283ac0f.md`) — blocks the remaining
   DB-backed pytest run and the UI-flow re-execution. **A repo CI workflow
   is now prepared and statically validated**
   (`.github/workflows/production-readiness-tests.yml`,
   `docs/CI_VERIFICATION_GUIDE.md`, §5 row 33) so the DB-backed suite can run
   on GitHub-hosted runners — but a hosted run has NOT been executed and
   requires the user's explicit activation/authorization; until then the
   ~720 DB-backed tests remain BLOCKED. The verifier does not bypass/weaken
   the policy. Workarounds that needed no policy change were applied where
   legitimate (`--noconftest` for DB-free tests; sync `env.py` for the
   Alembic chain, §1.1) — the migration revalidation is therefore complete;
   what remains blocked is the **DB-backed pytest suite** and the **UI-flow
   re-run**.
3. **UI-acceptance re-run** once the policy blocker is resolved — a single
   `pytest` invocation of the prepared 7-flow file; the kiosk flow already
   passed live. (Its HTTP replay needs the app's async session handling —
   not greenlet-free.)
4. **Full-suite re-run** (797 collected; §1.6/§1.7) on an eligible machine;
   77 greenlet-free tests already executed green (rows 29–31); the remaining
   DB-backed and fixture-backed tests are pending.
5. **Production `.env` authoring:** real secrets (≥32-char random), 
   `APP_ENV=production`, `APP_DEBUG=false`, real MySQL user (not root),
   Redis URL for multi-worker scale-out, payment/mail/Twilio credentials, AI
   provider config.
6. **Rotating local dev secrets** if the session transcript (§2.2) leaves the
   team.
7. ~~**Persistent logging solution** (file sink or collector) before launch~~
   — **RESOLVED Nov 7–8:** `LOG_DIR`/`LOG_FILE_LEVEL` settings + loguru
   rotating file sink (20 MB × 5 files) wired into `app/main.py` lifespan;
   compose `app_logs` volume mounted.
8. ~~**Backup/restore rehearsal:** never executed~~ — **PARTIALLY RESOLVED
   Nov 8:** first live execution ALL-OK (§5 row 41) in the disclosed
   `sync-create_all` fallback mode; a real-alembic-chain rehearsal remains
   queued (blocker matrix item #3). Legal/compliance review (SEC-009)
   remains an outstanding human obligation (handover:
   `docs/SEC009_LEGAL_HANDOVER.md`).
9. ~~Reconcile compose `mysql:8.3` with the production server version~~ —
   **RESOLVED Nov 7:** compose db + backup services pinned to
   `mysql:8.0.46`, the engine verified by every local/hosted migration run.

---

## 7. Prepared verification checklist for an approved Windows test environment / authorized CI runner

Ordered so each step reuses the previous step's disposable resources. Do not
bypass, disable or weaken Windows Code Integrity or any other security
policy; if the runner blocks a dependency, stop that check and record it.

1. **Environment gate:** confirm the venv's native modules load
   (`python -c "import greenlet"`) — the gate the current workstation fails.
   Alternative: enable the prepared CI workflow (§5 row 33) on the
   repository, which provisions its own Linux environment.
2. **Migration 0018 revalidation** — ✅ **completed in session 4 without
   greenlet** (§1.1): a disposable-DB sync `env.py` re-executed the real
   chain (up/down/re-up, 14 FKs, 9/9 indexes, legacy rows, no error 1553).
   No further migration action required unless the migration code changes.
3. **Production configuration tests:** rerun the 20-case matrix
   (`config_matrix.py`; §1.5/§1.7) plus the regression file
   `tests\test_config_security.py` (already 18/18 under `--noconftest`)
   under plain pytest once the full suite runs.
4. **UI acceptance flows:** recreate the 7-flow acceptance file (kiosk +
   receipt/QR security already passed live Nov 5; the completed fixture
   covered: org workflow config HTML+JSON, user manual booking + owner-only
   tracking + authz denials, employee stage progression (shift→advance→
   mandatory-skip refusal→skippable skip→recall→history), walk-in receipt
   ref, manual→AI handoff + confirmation gating + booked terminality +
   AI→manual prefill). Run all six pending flows to green.
5. **Full test suite:** record passed / failed / skipped / blocked counts
   and warnings; 797 tests are expected to collect; compare to the Phase-9
   baseline (779 passed / 775 passed runs). Investigate every failure; do
   not loosen assertions to obtain green.
6. **Configured LLM provider** only under written authorization: staging
   endpoint, metered short calls, one imported then refused confirmation
   round-trip; assert no booking is created and the API key never appears in
   logs. Never charged transactions, no real bookings or payments.
7. **Deployment config review completion:** persistent logging (file sink /
   rotation), log redaction of secrets/PII, compose `mysql` version
   reconciliation, backup/restore rehearsal.

Items 1–5 together, plus item 6, close every gap listed in §6 and would move
the project to a `READY FOR FINAL REVIEW` judgment (conditional on clean
results).

---

## 8. Recommended deployment steps (conditional ordering)

1. Author production `.env`: strong random `SECRET_KEY`/`JWT_SECRET_KEY`
   (`secrets.token_urlsafe`), strong `DB_PASSWORD` for a dedicated
   `skipline_user`, `APP_ENV=production`, `APP_DEBUG=false`, real
   Razorpay/Twilio/mail values, `REDIS_URL` when scaling beyond one worker,
   `AI_*` (or leave empty to ship manual-only booking), `NGINX_CONF=
   nginx.production.conf`.
2. Run the migration against the production MySQL **by the operator** (0018 up/down/up is now verified 8.x-safe; do
   not downgrade the production DB without a backup).
3. Stage TLS certs via certbot as documented; verify HSTS and the HTTP→HTTPS
   redirect fire.
4. `docker compose up --build -d`; confirm `/health`, `docker compose ps`
   healthy for app/db/redis(/clamav if enabled), then smoke-test:
   kiosk booking + signed receipt + QR, employee shift→stage lifecycle.
5. Start with the AI provider **disabled** (`AI_PROVIDER` empty) and enable
   it only after the live-LLM check (blocker 1) passes; the assistant
   degrades gracefully either way.
6. Enable persistent log shipping/rotation; verify scan_status semantics if
   uploads are used; exercise the backup profile and restore once.

---

## 9. Master requirements traceability (Nov 6)

Focus of this re-check: the two requirements explicitly retained by the
verification contract, plus the verification-evidence status of the
requirement areas affected by this session. No new feature audit was
performed (Phase-9 acceptance already covered features).

| Requirement (ref) | Implementation location | Verification evidence | Status |
|---|---|---|---|
| **Exactly four Platform Super Admin accounts** (Master §3.1 / checklist §86) | `app/services/admin_service.py` — `MAX_SUPER_ADMIN_ACCOUNTS = 4`, enforced in `create_super_admin` (raise at a 5th active); deactivation lowers the count so an authorized replacement is possible | committed test `tests/api/test_phase1_milestone.py::test_super_admin_four_account_limit` (creates 4, refuses 5th, deactivates, replaces); public registration cannot create Super Admin (Phase-9 evidence); execution re-run BLOCKED (§1.6) | VERIFIED (code + committed tests) — re-run pending |
| **Authorized dual approval for high-risk actions** (Master §4.4) | `app/services/enforcement_service.py` (second-person review policy, §4.4), `app/routers/admin.py` (suspension + reinstatement require a **distinct** second approver via `_verify_second_approver`; `SuspendOrgIn.second_approver_email/password`), `app/schemas/enforcement.py` (dual-approval schema) | committed tests `tests/unit/test_phase8_enforcement.py`, `tests/api/test_phase8_e2e.py` (Phase-8); same approver refused as different from the initiator | VERIFIED (code + committed tests) — re-run pending |
| Production secret enforcement (§24-adjacent) | `app/config.py` validators (§3.2) | 4→5-case suite + **20/20 matrix** + **18/18 pytest items** + committed regression file | VERIFIED (executed) |
| Migration 0018 MySQL compatibility | `migrations/versions/0018_workflows_and_ai.py` | Nov 5 chain evidence (§1.1) **retained**; **re-executed Nov 6 greenlet-free** on disposable MySQL 8.0.46 (up/down/re-up, 14 FKs, 9/9 indexes, legacy rows, no 1553) | VERIFIED (re-executed) |
| AI booking-safety (no LLM booking authority, confirmation gating) | `app/services/ai_*` | Phase-9 evidence; mock suite 20/20 + transport 9/9; **live LLM NOT VERIFIED** | PARTIALLY VERIFIED |
| UI acceptance flows (six pending) | app UIs/routes | kiosk flow PASSED live; six flows BLOCKED (§1.4) | BLOCKED |
| Backup/restore validation | `scripts/backup.sh` (gzip + `sha256` + min-size + retention checks), `scripts/restore.sh`, `docs/deployment.md`, `scripts/backup_restore_rehearsal.py` | **Executed live Nov 8 — ALL-OK** (readiness §5 row 41): dump/restore/full-parity/owned-teardown verified on live MySQL 8.0.46 in the banner-disclosed `sync-create_all` fallback mode; real-alembic-chain rehearsal queued behind the greenlet block | VERIFIED (fallback mode disclosed; real-chain run queued) |
| Legal/compliance prerequisites (SEC-009) | `docs/FEATURE_REQUIREMENTS_MATRIX.md` row SEC-009; handover `docs/SEC009_LEGAL_HANDOVER.md` | requirement text + implemented-control inventory + unresolved-questions list + approval block prepared for the human reviewer | NOT VERIFIED (human obligation — approval block unsigned) |

---

## 10. Architecture & environment addendum (Nov 8, 2026)

Full evidence: `docs/ARCHITECTURE_COMPATIBILITY_REPORT.md`. Summary of the
resolution of the workstation architecture failures:

- **Root cause of the local test blockage is NOT ARM64 incompatibility.**
  Windows Code Integrity policy `{0283ac0f}` blocks the unsigned ARM64
  `_greenlet.cp312-win_arm64.pyd` specifically (event 3077); the identical
  vendor's unsigned AMD64 build imports fine under WoA x64 emulation, and the
  same pinned greenlet runs natively inside the ARM64 Linux container. The
  venv itself was verified arch-consistent (all native `.pyd` ARM64).
- **Environment decision:** keep ARM64 Python 3.12 venv for greenlet-free
  work; use the Docker Desktop **ARM64-native Linux engine**
  (ServerVersion 29.8.1, Arch=aarch64) for greenlet/architecture-sensitive
  verification; hosted `ubuntu-latest` CI stays authoritative. No Pythons
  installed/uninstalled; existing `venv` untouched.
- **Container execution evidence (all ARM64-native, `python:3.12-slim`
  arm64 + pinned wheels incl. aarch64 greenlet/pydantic-core/Pillow):**
  1. Real Alembic chain 0001→0018 via `scripts/ci_migration_chain_check.py`
     inside a network-namespace-shared checker container against a fresh
     disposable MySQL 8.0.46 (aarch64): **ALL-OK, exit 0** (14/14 FKs, 9/9
     indexes, no 1553, legacy rows preserved, script dropped its own DB).
     Corroborates — does NOT replace — run-9 backup/restore rehearsal.
  2. App bring-up through `scripts/start.sh` (alembic upgrade head +
     uvicorn): `/health` → `{"status":"ok","app":"Skip Line"}`, probe DB
     `alembic_version=0018`. **PASS**.
  3. Container full pytest suite: see row 44 below.
- **New disposition, item 7 of §7 above (persistent hardening): verified
  partially container-side** — the app image boots with rotating-file logging
  config intact; local re-verification remains policy-blocked as before.
- **Protected data:** `skipline`, `skipline_phase9_prodcheck`,
  `skipline_synccheck` (and unrelated user DBs) never touched; all container
  probes used the isolated `skipline_probe_net` network and synthetic
  credentials; no git operations performed.

### Ledger rows 43–44

| # | Closure | Date | Evidence |
|---|---|---|---|
| 43 | **Windows-on-ARM architecture audit + reproducible environment standardization** | Nov 8 | PE-header verified interpreters (ARM64 3.12.10 base; AMD64 3.13.15 runs under WoA x64 emulation); venv arch-consistent; WDAC pilot: unsigned AMD64 greenlet import PASS vs unsigned ARM64 greenlet BLOCKED (policy `0283ac0f`, per-arch nuance); Docker manifest audit: 4/5 compose images multi-arch (clamav 1.4 amd64-only, optional service); Docker engine ARM64-native; decisions + rejected alternatives + repro commands in `docs/ARCHITECTURE_COMPATIBILITY_REPORT.md` | 
| 44 | **Docker ARM64-native verification bring-up** | Nov 8 | `skip-line-app:1.01GB` built locally exit 0 (aarch64 wheels); redis probe `redis-cli 7.4.12` PASS; fresh disposable MySQL 8.0.46 probe healthy ~5 s; **real alembic chain ALL-OK exit 0** in-container (§9.2 of the architecture report); app `/health` smoke PASS with `start.sh`-driven `alembic upgrade head` (probe DB `alembic_version=0018`); **security fix: image previously contained the real `.env` (no `.dockerignore`) — `.dockerignore` created, rebuild verified `/app/.env` ABSENT**; **container full suite PASS: junit tests=864 failures=0 errors=0 skipped=4 (real-Redis), exit 0, 63.6 s** (acceptance modules excluded: `playwright` not in pinned `requirements.txt` — collection BLOCKED by package absence in-container, not by WDAC); stale duplicate `tests/unit/test_config_security.py` surfaced by the container run and minimally aligned (`app_debug=False` deterministic default) | local Docker Desktop ARM64 engine, disposable probe network only |
| 45 | **Credential origin, validity & production configuration audit (read-only)** | Nov 8 | `docs/CREDENTIAL_ORIGIN_AUDIT.md`. Origin: owner confirmed supplying NO credentials — `.env` (untracked, **0 commits in full history**) is OpenCode-generated end to end. Redaction-safe classifier (`skipline_credential_origin_audit.py`, values never printed): 38 active keys; only **real working credentials** = DB root password (32-ch mixed random; **CONNECTIVITY VERIFIED** via whitelisted read-only probes: SELECT 1/CURRENT_USER/SHOW DATABASES — app DB `skipline` present; protected `skipline*` DBs seen untouched) + generated dev `ADMIN_PASSWORD`. All external integrations = placeholders or intentionally absent; **no provider (LLM/payments/email/SMS) ever contacted by the app**; AI fail-safe verified by tests, mail/SMS honest pending-outbox behavior, Razorpay fail-late. **Production blockers are human-supplied items:** stable `SECRET_KEY`/`JWT_SECRET_KEY` (both are dev placeholders — production validators reject by design), dedicated least-privilege DB user (app runs as root today; app never creates users), rotated root password + `MYSQL_ROOT_PASSWORD` for compose (currently MISSING from `.env`; `.env` is a host-MySQL dev config — compose would reject `MYSQL_USER=root`), Razorpay/SMTP credentials if used, Twilio wiring NOT IMPLEMENTED. New defect flagged: `init.sql` hardcodes `skipline_pass` (inert under compose first-boot; recommend parameterization — not changed in this read-only audit). Post-fix image layer note: `.dockerignore` closes the Docker-layer exposure; owner-run `docker system prune` recommended for the old unreferenced cache layers | local, read-only; probes whitelisted SELECT/SHOW only |
| 46 | **MySQL root password origin investigation + safe non-executing remediation plan** | Nov 8 | `docs/MYSQL_ROOT_PASSWORD_ORIGIN_REPORT.md`. Origin **CONFIRMED, not UNRESOLVED**: the current `root@localhost` password is OpenCode-generated (Python `secrets`) **and OpenCode-applied server-side** on **2026-10-31 22:54:53** via `rotate_db.py`, during an owner-authorized rotation of a Git-history-leaked credential (original hardcoded in `app/config.py` since pre-rewrite commit `82d6d45`, pushed to origin pre-cleanup; history then rewritten, tag `v1.0-baseline` → clean `e65360d`). Byte-exact proof: `mysql.user` `password_last_changed` == `.env` mtime (same second). Leaked blob now exists **only** as an orphaned local object (reachable by no ref). **Incident disclosed:** one mask-regex miss printed the current root password in the audit transcript (Nov 7 acceptance `.out` log; value still on disk) → **REQUIREMENT (R1): immediate operator rotation of the root password; delete the `.out` after**. Remediation plan R0–R9 (never executed by the audit): R0 dump backup → R1 rotate root → R2 dedicated `skipline_app`@`localhost` least-privilege user (grants incl. **REFERENCES** — required for MySQL 8.0 FKs, missing from `init.sql` — plus SHOW VIEW/TRIGGER/LOCK TABLES) → R3 dev/prod credential separation, root out of `.env` → R4 `MYSQL_ROOT_PASSWORD` compose config (**first-init-only volume caveat**) → R5 parameterize/remove hardcoded `skipline_pass` in `database/init.sql` → R6 data-preservation guarantees → R7 optional local hygiene (orphan purge, log cleanup) → R8 validation sequence (least-privilege probes, protected-DB deny, `/health` smoke, migration chain on app account) → R9 rollback table. Strict restrictions respected | read-only; no password/user/DB/`.env`/git/Docker-destruction ops performed |
| 47 | **WP1 Production Readiness Closure — six-flow acceptance CONVERGED + real-scripts backup/restore rehearsal PASS + full regression PASS** | Nov 12 | **(a) Acceptance 7/7 PASS** after 13 evidence-driven runs (junit run 13: tests=7 failures=0 errors=0, 118 s; canonical `%TEMP%\opencode\wp1_acceptance_junit.xml`). **3 genuine APP defects found and fixed by the flows** (Task 2 authorized): `tokens.py` download_receipt MissingGreenlet → explicit `db.get(Branch,…)`; `schedule_service.get_public_slots_for_date` spurious 404 for legacy day-queue counters (dropped `queue_mode != SLOTS` from the guard; downstream query already filters SLOTS); `employee.get_counter_queue_api` nonexistent `TokenStatus.SERVING` → AttributeError 500 on every queue poll → real member set `WAITING/CALLED/IN_PROGRESS/ON_HOLD`. Harness fixes documented with run evidence in comments (PDF receipts via `page.context.request.get` 200+`application/pdf` / uniform 404 cross-user; FIFO queue isolation + em-dash-proof positive assertion; "Next Stage" label; async dialog handler; conftest `@acceptance.dev` emails — pydantic v2 `EmailStr` rejects RFC-reserved `.test`; httpx 0.27 cookie jar mapping; flow6 conversation_id capture). Sync private-event-loop server fixture (daemon thread) permanently removed the greenlet/WDAC dependency — no bypass used. **(b) `database/init.sql` R5 fix:** hardcoded `skipline_pass` removed (Task 4; 10 unit tests green; `requirements-acceptance.txt` pins `playwright==1.64.1`). **(c) Backup/restore rehearsal PASS** via the REAL production `scripts/backup.sh`/`restore.sh` (**unmodified**) + real alembic async chain 0001→0018 (15.2 s): artifact 12,214 bytes sha256 `6ebe06c4…7b64`; restore → parity **6/6 MATCH** (65 tables / 230 indexes / 134 FKs / alembic_version=0018 / token rows RH-0001+RH-0002 / waiting tokens); app-level read on restored DB (real uvicorn `/health` 200 + async ORM = 2 WAITING tokens, both stages); owned teardown dropped both `skipline_wps_*` DBs; protected `skipline*` DBs never touched (guard refuses overlap). Evidence: `%LOCALAPPDATA%\Temp\opencode\wp1\rehearsal_evidence.json` stamp 20261112152349. Closes §10-acceptance blockers #1 (flows) and #3 (real-chain rehearsal). **(d) Full regression (Task 5): junit tests=874 failures=0 errors=0 skipped=4 (real-Redis self-skips), exit 0, 97.9 s** (`%TEMP%\opencode\wp1_regression_junit.xml`). Verdict impact: acceptance report §1 item "flows BLOCKED" resolved; verdict B remains pending human items only (#2 full compose, #4 live LLM credentials, #5 SEC-009 signature, #6 hosted CI rerun on the pushed changeset) | local workstation venv (ARM64), disposable `skipline_wps_*`/`skipline_acceptance_*` DBs only; all work uncommitted on preserved tree (0 staged) |

---

*Report generated from the verification transcript.*

