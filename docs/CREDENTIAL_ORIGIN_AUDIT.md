# SkipLine — Credential Origin, Validity & Production Configuration Audit

Date: Nov 8, 2026 · Read-only audit (nothing modified, no git operations,
no secrets displayed, no external calls made).
Baseline: `f44c320` (unchanged); audit artifacts in
`%TEMP%\opencode\prodcheck\` (`skipline_credential_origin_audit.py` output
shows classifications/booleans only — secret values never leave the process).

Per the owner-provided premise: **the owner never supplied OpenCode with any
database, API, payment, email or SMS credential.** Repo evidence confirms:
`.env` has **0 commits in full Git history** (`git log --all -- .env` is
empty) and was produced by the OpenCode-driven setup sessions; every value in
it is therefore **OpenCode-generated** configuration.

## 1. Database credentials

**Origin chain (evidence):**
- `app/config.py`: `db_user`/`db_password` required settings; URLs assembled
  via `quote_plus(db_password)` into `mysql+aiomysql://…` (async, `app/database.py`)
  and a sync URL for alembic. **No default password exists in code**
  (`.env.example` states this; validator `validate_db_password` enforces).
- `docker-compose.yml`: `db` service creates the app DB/user at *first boot*
  via `MYSQL_DATABASE/MYSQL_USER(=DB_USER)/MYSQL_PASSWORD(=DB_PASSWORD)`; both
  `MYSQL_ROOT_PASSWORD` and `DB_PASSWORD` are `${VAR:?required}`-guarded.
- `database/init.sql` also creates `'skipline_user'@'%'` — **with a hardcoded
  placeholder password `'skipline_pass'`** (only `CREATE USER` in the repo).
  Under the compose flow it is inert (`IF NOT EXISTS` + the entrypoint already
  created the real user); **standalone use of init.sql would deploy a
  known-weak credential** — flagged defect, recommend env-driven parametrization.
- **Competence boundary:** the application NEVER creates DB users — it assumes
  they exist (`SELECT COUNT(*) FROM mysql.user` shows exactly one `root` row
  on the host; no repo-side provisioning for the host server).

**The verified credential (read-only probes only — SELECT 1 / CURRENT_USER /
SHOW GRANTS / SHOW DATABASES; schema/SHOW mysql.user count):**
- `.env` `DB_USER=root`, `DB_PASSWORD` = 32-char mixed random → **CONNECTIVITY
  VERIFIED** on the host MySQL 8.0.46 (localhost). The generated password
  *matches the actual server root account* and the app DB `skipline` exists;
  11 databases visible incl. the locally protected `skipline`,
  `skipline_phase9_prodcheck`, `skipline_synccheck` (untouched by the probe).
- So: real, working, OpenCode-generated **local development** credential. It
  is a *root* credential — convenient for this workstation's dev/host-MySQL
  setup, unacceptable as a production identity.
- Compose compatibility note: `MYSQL_USER` fed from this `DB_USER=root` would
  be rejected by the MySQL image entrypoint (root is not user-creatable), so
  **the current `.env` is a host-MySQL dev configuration, not a compose config.**

Distinguishing local development vs production credentials: everything in
`.env` is local-dev; production `.env` is a human task (§3 validators refuse
insecure markers, debug mode, missing/short secrets in `APP_ENV=production`).

## 2. External API credentials

| Integration | Env vars | State in `.env` | Real credential ever configured? | Provider call ever executed? | Fail-safe behavior (code-verified) |
|---|---|---|---|---|---|
| AI assistant (OpenAI-compatible) | `AI_PROVIDER`, `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`, timeouts | **ABSENT (intentional)** | No — no `AI_*` keys ever existed in `.env` (secret-scan row 42) | **NOT APPLICABLE / never** (no credentials ⇒ mock-only; 9/9 mock transport + 6/6 resilience) | Graceful unavailable: `is_configured()` false ⇒ every `/ai` endpoint responds "unavailable"; provider failures degrade; API key never logged (tested). Live-LLM test requires written authorization (blocker #4) |
| Razorpay payments | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `RAZORPAY_CURRENCY` | **GENERATED PLACEHOLDER** (`rzp_test_…`-shaped ID + placeholder secret/webhook) | No | Never (no paid calls by rule) | Fail-late: `_razorpay_client()` builds a client from settings; an order with placeholder/empty keys fails at provider auth (error surfaced to caller, not a graceful "unavailable" like AI). Webhook signature check would reject placeholders. Payments only execute when a payment flow is explicitly invoked |
| Email | `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_SERVER`, `MAIL_PORT`, `MAIL_STARTTLS`, `MAIL_FROM(…)` | **GENERATED PLACEHOLDER** (username/password) | No | Never | Fail-safe: empty username ⇒ skip with warning, `False`, honest pending outbox row; placeholder (non-empty) values would attempt SMTP and fail into error-log + outbox attempt state (never claimed delivered). Redaction (`minimal_preview_text`) on subjects/bodies |
| SMS (Twilio) | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | **GENERATED PLACEHOLDER** | No | Never — **integration is NOT IMPLEMENTED end-to-end**: the send function returns `False` on a documented placeholder path and *never calls a provider*; delivery is never faked | Honest pending outbox rows (NTF-002) |
| Redis (queue/rate-limit) | `REDIS_URL` | Not set (commented out) → compose default `redis://redis:6379/0` | n/a (no credential) | Container smoke: redis-cli 7.4.12 PASS (ARM64) | Empty ⇒ single-worker in-process mode (documented) |

## 3. Production security posture

- **Weak/predictable/shared/hardcoded:** no hardcoded production secrets
  (secret-scan row 42: all third-party values are placeholders; CI contains only
  `ci-only-synthetic-password`; backups decompress to 0 credential matches).
  Two dev-only artifacts remain: `init.sql` `'skipline_pass'` (inert under
  compose, dangerous standalone) and the placeholder `SECRET_KEY`/`JWT_SECRET_KEY`
  used in dev **with a RuntimeWarning but NOT rotated** (dev identity-document
  encryption then runs on a weak key; production rejects it hard).
- **Placeholder rejection in production (code + tests):** `_is_unfilled_marker`
  + `_INSECURE_*` lists; `validate_db_password` required; ≥32-char secret
  minimum; `APP_DEBUG=true` + production raises; weak `ADMIN_PASSWORD`
  rejected (`Admin@1234!`/`admin`/`password`/short). Evidence:
  `tests/unit/test_config_security.py` + `tests/test_config_security.py`
  (green in the ARM64 container run: junit 864/0/0, Nov 8).
- **Exposure surfaces:** Git — `.env` never committed (0 commits, full
  history); **Docker layers — the un-`ignore`d `.env` WAS baked into the app
  image (found Nov 8); fixed same day: `.dockerignore` created, rebuild
  verified `/app/.env` ABSENT.** Residual: the pre-fix build layers still sit
  in Docker's local build cache (unreferenced) — recommend an owner-executed
  `docker system prune` (deliberately NOT run during this read-only audit).
  Test outputs/logs: dummy values only; AI key-never-logged assertion passes;
  URL strings redacted in error paths.
- **Sharing:** the verified root credential is shared by app + DB superuser
  (dev-only). Production must use a dedicated least-privilege app user
  (the `skipline_user` model exists) and a rotated root password nobody's
  app knows.

## 4. Final credential table

Values never displayed.

| Credential | Origin | Current status | Verified connectivity | Production readiness | Required action |
|---|---|---|---|---|---|
| `DB_USER`(=`root`) + `DB_PASSWORD` (32-ch mixed random) | OpenCode-generated during local bootstrap | **CONNECTIVITY VERIFIED** (host MySQL dev) | YES — read-only whitelist probe authenticated; app DB `skipline` present | **NOT production-ready** — root superuser identity; dev-only | HUMAN: provision dedicated least-privilege app user; rotate the dev root password; supply separate production DB credentials |
| `MYSQL_ROOT_PASSWORD` (compose) | MISSING from `.env` (provider required by compose) | **MISSING** | n/a (synthetic-only in probe bring-ups) | Required only for the compose stack | HUMAN: generate unique per-environment root password |
| `SECRET_KEY` | OpenCode-generated **placeholder** | **GENERATED PLACEHOLDER** (dev: used with warning, weak key) | n/a — local only by design | **FAILS production validators by design** | HUMAN: set stable `secrets.token_urlsafe` value |
| `JWT_SECRET_KEY` | OpenCode-generated **placeholder** | **GENERATED PLACEHOLDER** | n/a | **FAILS production validators by design** | HUMAN: set stable random value |
| `ADMIN_PASSWORD` (dev Super Admin bootstrap) | OpenCode-generated strong (25-ch mixed) | **CONFIGURED BUT UNVERIFIED** (dev-local login only) | In-app seed/login only (dev) | Replace for production (validators would accept strength but it is a known dev credential) | HUMAN: production admin credentials + first-run `ADMIN_PASSWORD` |
| AI provider (`AI_*`) | ABSENT from `.env` (intentional) | **MISSING** (gracefully disabled) | NOT APPLICABLE — never attempted | Ship-safe with AI off | HUMAN (optional): authorized provider account + keys, then live round-trip test |
| Razorpay (`RAZORPAY_*`) | OpenCode-generated placeholders (`rzp_test_…` shape) | **GENERATED PLACEHOLDER** | NOT APPLICABLE — never attempted (no authorization) | Order creation would fail at provider auth | HUMAN: real test/live keys + webhook secret (or ship manual-payments-only) |
| Email (`MAIL_*`) | OpenCode-generated placeholders | **GENERATED PLACEHOLDER** | NOT APPLICABLE — never attempted | Sends would fail into honest outbox error state | HUMAN: real SMTP/mail credential |
| Twilio (`TWILIO_*`) | OpenCode-generated placeholders | **GENERATED PLACEHOLDER** + integration NOT IMPLEMENTED | NOT APPLICABLE — provider never called (documented placeholder path) | Not wired at all | HUMAN: implement provider wiring (or accept SMS-free deployment) |
| `skipline_pass` literal | Hardcoded in `database/init.sql` | **GENERATED PLACEHOLDER** (known weak) | Inert under compose first-boot flow | DO NOT rely on standalone init.sql in production | Fix: parameterize the init-SQL user creation or delete the literal (recomm. change, not performed in this read-only audit) |
| `REDIS_URL` / `CLAMAV_*` / misc routing | Compose defaults / optional toggles | Not applicable (no credentials) | redis probe PASS (7.4.12, ARM64) | Compose default OK | None (docker-only) |

## Conclusion

The project currently has **exactly one working, real credential** — the
OpenCode-generated MySQL **root** password for the local development server
(read-only verified), plus a working dev admin password. Everything else is
generated placeholder or intentionally absent configuration; **no external
provider (LLM, payments, email, SMS) has ever been contacted by this
application** (no credentials exist, so it cannot; all integrations are
fail-safe or honestly disabled).

**What the owner must supply before production deployment:**
1. Production `.env` — stable ≥32-char `SECRET_KEY` and `JWT_SECRET_KEY`
   (random, not the dev values); strong `APP_` production flag set;
   `ADMIN_PASSWORD` unique to production.
2. A dedicated least-privilege MySQL app user (not root) + its password, and a
   *rotated* MySQL root password distinct from the leaked-in-public-history
   dev one; supply `MYSQL_ROOT_PASSWORD` for the compose stack only.
3. Razorpay keys (test first, then live) + webhook secret — and a live
   round-trip verification policy.
4. Real SMTP/mail username/password (and receptor inbox) — or explicitly
   accept the pending-outbox behavior without email.
5. OPTIONAL under written authorization: OpenAI-compatible AI credentials;
   Twilio only after the provider wiring is implemented.

No placeholder value should ever be promoted anywhere: production validators
make that technically impossible for the in-startup paths, but the operator
procedure above is the human control that actually completes the loop.

---

## WP1 addendum (Nov 12, 2026)

Append-only closure notes; still read-only with respect to credentials —
no `.env` edit, no password/user/account change, no credential generation,
no secret value printed.

1. **`database/init.sql` defect (§1 above) — FIXED (WP1 Task 4).** The
   hardcoded `'skipline_pass'` placeholder was removed from
   `CREATE USER 'skipline_user'@'%'` (now parameter-free of literal
   passwords; 10 unit tests green). This implements R5 of the root-password
   origin report's remediation plan.
2. **MySQL root password origin (Task 1) — CONFIRMED, unchanged.** As
   documented in `docs/MYSQL_ROOT_PASSWORD_ORIGIN_REPORT.md`: OpenCode
   generated and applied the current `root@localhost` password on
   2026-10-31 via `rotate_db.py` (owner-authorized rotation of the
   Git-history-leaked credential). No re-run was needed; the origin report
   stands as evidence.
3. **R1 (root rotation) remains MANDATED but sequenced** after the R2
   least-privilege account transition (plan C0–C8); not executable within
   WP1's restrictions (no MySQL password/user/account changes). The
   transcript file containing the disclosed mask-regex miss is an owner
   cleanup item.
4. **WP1 verification evidence produced** (none of it touches credentials):
   six-flow Playwright acceptance 7/7 PASS (run 13; three genuine app
   defects fixed en route), real-scripts backup/restore rehearsal PASS
   (`%LOCALAPPDATA%\Temp\opencode\wp1\rehearsal_evidence.json`, parity
   6/6 MATCH on disposable `skipline_wps_*` DBs), full regression 874
   passed / 0 failed / 4 real-Redis skips. Protected DBs
   `skipline_phase9_prodcheck` / `skipline_synccheck` (and `skipline`)
   were never modified or deleted; rehearsal teardown dropped only its own
   uniquely-named `skipline_wps_*` databases.

---

## WP2 addendum (Nov 12, 2026) — R0–R9 EXECUTED, root rotation + least-privilege transition CLOSED

Executed by `scripts/wps2_privilege_transition.py` (+ `wps2_privcheck_verify.py`,
`wps2_redact_root_leaks.py`) on baseline HEAD `c2fa863e38643f148cbf8f91b7222a2a3345c22f`,
working tree clean at start, **all WP2 changes left uncommitted** for owner
review. No secret value was ever printed or written to any evidence file
(masked `***` throughout; evidence:
`%LOCALAPPDATA%\Temp\opencode\wp2\wps2_evidence.json`).

| Step | Result |
|---|---|
| **R8-redact (run FIRST, before rotation)** | The compromised root credential was redacted from disk while still known-in-memory: 2 shell-transcript artifacts (`sh_1b4e0fa3e001pRH491F2PKXq22.out` — the Nov 7 artifact referenced above — and `sh_1bae58bb6001f75mGgEw2dCLpz.out`) redacted in place with `***REDACTED-WP2***`. Order deviation from the task list (R8 after R1) is deliberate: it removes the compromised value from disk in the same process that rotates it. |
| **R0 pre-transition backup** | `mysqldump` of exactly the three protected databases → `pre_wp2_backup_20261112T102242Z.sql` (183,739 bytes, sha256 `b5b2ac5b…`, `Dump completed` footer verified; credential passed via `MYSQL_PWD` env var, never on the command line). |
| **R2/R3 least-privilege account** | `skipline_app`@`localhost` provisioned (same `caching_sha2_password` plugin as root). SHOW GRANTS: `USAGE ON *.*` (zero global privileges) + 4 pattern grants (`skipline.*`, `skipline_acceptance\_%`, `skipline_wps\_%`, `skipline_test\_%`) with exactly SELECT/INSERT/UPDATE/DELETE/CREATE/ALTER/DROP/INDEX/REFERENCES/CREATE TEMPORARY TABLES/LOCK TABLES. Zero banned privileges (SUPER/GRANT OPTION/FILE/PROCESS/SHUTDOWN/RELOAD). Empirical DDL probe: the pattern grants allow CREATE/DROP of disposable probe DBs. **Documented deviation:** no `@'%'` host variant was created — compose's `app` service connects to the compose-internal `db` container (its own MySQL, user created by the entrypoint from `MYSQL_USER`), the host MySQL is loopback-only, and the Docker daemon is offline locally; add the `@'%'` grant at compose bring-up if the containerized path requires it. |
| **R1 root rotation** | `root`@`localhost` rotated (`ALTER USER … IDENTIFIED BY` + `FLUSH PRIVILEGES`). New credential VERIFIED by authentication; the old compromised credential REJECTED with 1045 Access denied immediately after. The old value is now unrecoverable (rotated + removed from disk) — this closes the R1 mandate of WP1 addendum item 3. |
| **R4 configuration** | `.env` rewritten atomically (temp file → `ALTER USER` success → `os.replace`; CRLF preserved, 66 lines): `DB_USER=skipline_app`, new app credential, `MYSQL_ROOT_PASSWORD` added (admin-only). `docker-compose.yml`: `MYSQL_USER`/`DB_USER` now `${DB_USER:?required}` guards in db + backup services (weak `skipline_user` fallback removed); `app/config.py` default `db_user` → `skipline_app`; `.env.example` documents the least-privilege identity with placeholder-only passwords; acceptance conftest fallback aligned. 14/14 unit tests in `tests/unit/test_db_least_privilege_config.py` lock this in. |
| **R7 least-privilege verification** | (a) Full alembic chain **0001→0018 in 47.09 s** on disposable `skipline_wps_privcheck_20261112102657` created/migrated/verified (alembic_version=0018, 65 tables)/dropped **entirely as `skipline_app`, zero root fallback** (`wps2_privcheck_evidence.json`). (b) Acceptance flows 1a/2/3/5 **4/4 PASS** (177.8 s) under the new identity — conftest now creates/seeds/tears-down its disposable DB as `skipline_app`; teardown clean, no leftover disposable DBs. (c) Post-transition: protected databases intact; runtime read as `skipline_app` against `skipline` OK. |
| **R8 residual-leak sweep (post-rotation, shape-based)** | Because the old value exists nowhere in memory or config after rotation, a credential-SHAPE scan (root URLs / `DB_PASSWORD=` / `IDENTIFIED BY` / `-p` literals, 20+ alnum) over all 360 shell-transcript and `%TEMP%\opencode` artifacts detected **0 remaining credential-shaped values**, and every detected value had been auth-tested before redaction (root auth attempt expecting 1045 — proving it was the dead rotated-out credential, never the new one). `LIVE credentials found: 0`. Evidence: `wps2_leak_redaction.json`. This closes the WP1 addendum item 3 owner-cleanup item (transcript `.out`). |

**Net state:** the application runtime identity is now the dedicated
least-privilege `skipline_app`; root is administration-only and its
compromised password is rotated out and absent from disk; every residual
disclosure artifact is redacted; the remediation plan **R0–R9 is closed**
except R3's compose bring-up items (Docker daemon offline locally —
compose hardening is in place and unit-tested; first containerized bring-up
remains a hosted-CI / owner gate, noting the first-init-only volume caveat
from the origin report). Phase 10 remains untouched.

---

## WP3 addendum (Nov 12, 2026) — application secret rotation (R3 closure, application layer)

Executed by `scripts/wps3_secret_hardening.py` on baseline HEAD
`9872cd50e16a3b1990745c6c3f7c80d73b4f13fb` (post-WP2-checkpoint), **all WP3
changes left uncommitted**. No secret value was ever printed or written to
evidence (masked `***`; evidence:
`%LOCALAPPDATA%\Temp\opencode\wp3\wp3_secret_rotation.json`).

1. **`SECRET_KEY` / `JWT_SECRET_KEY` rotated** — the two dev placeholder
   values were replaced with distinct 64-character cryptographically random
   alphanumeric secrets (generated in-process via `secrets`; strictly
   distinct from each other and from `ADMIN_PASSWORD`). `.env` was rewritten
   atomically (temp file → `os.replace`), CRLF preserved (66 lines), all
   other keys — including the WP2 `skipline_app` / `MYSQL_ROOT_PASSWORD`
   identities — untouched. Production-mode validation
   (`Settings(app_env="production", app_debug=False)`) was simulated
   in-process with warnings-as-errors: **zero weak-secret errors/warnings**.
   Note: rotation invalidates all previously issued JWTs and any values
   signed with the old `SECRET_KEY` (sessions re-authenticate).
2. **`ADMIN_PASSWORD` rotated — including the LIVE seeded account.** The
   `.env` value alone only seeds when NO admins exist
   (`seed_default_admin` is a no-op otherwise), so the already-seeded
   default Super Admin in `skipline.admins` was rotated in the same
   operation using the app's own `hash_password`: the old dev-era hash was
   overwritten, the NEW password verifies, and the old development password
   is **rejected** (booleans verified; account id not printed). The old dev
   ADMIN_PASSWORD is now unrecoverable (overwritten in `.env` and its hash
   replaced in the DB) — closing the "generated dev ADMIN_PASSWORD" live
   credential noted in the Nov 8 origin audit (Row 45).
3. **First-run note (idempotent recovery):** the very first driver run
   aborted on a module-path error AFTER the `.env` rewrite but BEFORE the
   DB rotation; the rerun force-set the admin hash idempotently and carried
   the post-rotation verification. No secret was exposed at any point.
4. **Optional provider keys (`RAZORPAY_*`, `MAIL_*`, `TWILIO_*`) remain
   non-empty placeholder values** — unchanged by WP3 (no real provider
   credentials exist; the app never contacted any provider, per Row 45).
   WP3 added graceful-degradation guarantees instead: unconfigured/failed
   Razorpay now yields a controlled `PaymentsNotConfigured` → HTTP 503
   (never a 500 traceback), and the durable outbox marks unconfigured
   channels `SUPPRESSED ("provider unconfigured")` with honest state and no
   retry churn (backoff 30/60/120 s is exponential; the ~100 ms loop is only
   the bounded commit-visibility poll). Owner decision (human): either fill
   real provider credentials (WP3 human gate) or blank the placeholder keys
   so the clean SUPPRESSED path engages.