# SkipLine — CI Verification Guide (Phase-9 production readiness)

This guide explains how the prepared GitHub Actions workflow executes the
complete automated test suite with real, disposable services, how to interpret
its output, and how to reproduce failures locally **on an authorized machine**.

Status of the underlying verification is tracked in
`docs/PRODUCTION_READINESS_REPORT.md`. CI preparation itself does **not** change
any test result: hosted-runner execution is pending explicit activation.

---

## 1. Workflow files

| File | Purpose |
|---|---|
| `.github/workflows/production-readiness-tests.yml` | Two health-gated jobs: (1) full pytest suite with disposable MySQL 8.0.46 + Redis 7 services, (2) real MySQL migration chain `0001→0018` up/down/re-up with FK/index audit on the repo's normal async `env.py`. |
| `scripts/ci_migration_chain_check.py` | Self-gating migration verification (§4 below). |

Activation model: the workflow defines `push`/`pull_request` (branches: main)
and `workflow_dispatch`, but nothing fires until Actions is enabled for the
repository by an authorized user (repo **Settings → Actions**). Executing hosted
runs also requires the user's explicit approval; the verifier never triggers
runs or changes repository settings.

## 2. Services provisioned per job

| Service | Image | Why | Isolation |
|---|---|---|---|
| MySQL | `mysql:8.0.46` (exact version verified locally) | migration-chain job creates and drops the exact allowlist DB `skipline_ci_migrate`; the pytest job's `skipline_ci` container DB exists but no pytest case connects to it | disposable container, synthetic root password, test DB dropped by script (guarded teardown) |
| Redis | `redis:7-alpine` | `tests/integration/test_redis_real.py` self-skips when unreachable — in CI it will genuinely run (default `REDIS_TEST_URL` = `redis://127.0.0.1:6379/15`, db 15 only) | disposable container, dedicated db number |

Both services are health-gated (`mysqladmin ping`, `redis-cli ping`) before the
test steps start.

## 3. Python and dependency setup

- Python **3.12** (matches the verified local environment; no other version
  has run the suite).
- `pip install -r requirements.txt` — every pin is fully versioned. All pinned
  packages were verified to exist and importable from the working local
  venv, and none are Windows-only (`tzdata` supplements zoneinfo on Windows
  and is harmless on Linux).
- **Fixed during CI preparation:** `aiosqlite==0.22.1` was required by
  `tests/conftest.py` (in-memory test DB) but missing from `requirements.txt`,
  and `greenlet==3.5.2` (hard dependency of async SQLAlchemy) was only
  installed conditionally by pip — both are now explicitly pinned in the
  file so CI and local installs cannot diverge.
- Known pin note: the local venv currently carries `redis 8.1.0` while the
  file pins `redis==5.0.4`. CI follows the file (it is the reproducible
  source of truth). If a redis-API incompatibility appears, re-pin deliberately
  — do not upgrade casually.

## 4. Environment variables (all synthetic)

| Variable | CI value | Purpose |
|---|---|---|
| `DB_HOST` / `DB_PORT` | `127.0.0.1` / `3306` | points the (never-connected) app engine at the disposable MySQL container |
| `DB_NAME` | `skipline_ci` (tests) · `skipline_ci_migrate` (migration job) | disposable DB names; the real project DB `skipline` is never addressed |
| `DB_USER` / `DB_PASSWORD` | `root` / `ci-only-synthetic-password` | CI container user; synthetic, not a real secret |
| `REDIS_TEST_URL` | `redis://127.0.0.1:6379/15` | routes the real-Redis integration tests to the isolated db 15 |

Secrets such as `SECRET_KEY` / `JWT_SECRET_KEY` / `ADMIN_PASSWORD` are **not**
required: `tests/conftest.py` installs test-only dummy values via
`os.environ.setdefault`. No GitHub repository secrets are used anywhere in the
workflow (verified statically), so fork pull requests cannot obtain anything
privileged, and untrusted fork code runs with `contents: read` only.

Never add production credentials, Razorpay/PayPal test-mode-with-real
accounts, Twilio keys, SMTP passwords, or paid LLM keys to this workflow. The
email path is always mocked in CI fixtures; no AI provider is configured on
CI (the assistant degrades gracefully and `AI_*` must stay unset).

## 5. Migration execution

Job 2 (`mysql-migration-chain`) runs `scripts/ci_migration_chain_check.py`:

1. **Safety gate (`validate_ci_config`, exact allowlist — pre-activation
   review, Nov 6):** refuses to run unless `DB_NAME` is **exactly**
   `skipline_ci_migrate` (an exact allowlist, NOT a prefix — the real project
   database `skipline`, production-like names, empty values and every other
   name are rejected before any connection is opened); `DB_HOST` must be
   loopback (`127.0.0.1` / `localhost` — so destructive SQL can never reach a
   remote server); `DB_PORT` must be an integer 1–65535; `DB_USER` and
   `DB_PASSWORD` must be non-empty (synthetic values only). Hermetic gate
   tests: `tests/test_ci_migration_gate.py` (`pytest --noconftest`).
2. Creates the disposable DB `skipline_ci_migrate` — but ONLY if it does not
   already exist on the server. `refuse_if_preexisting()` rejects a
   pre-existing `skipline_ci_migrate` and leaves it entirely untouched
   (final destructive-safety review, Nov 6: the script never drops a
   database its current run did not create, and there is no unconditional
   `DROP IF EXISTS` before creation anywhere).
3. `alembic upgrade 0017` (real chain 0001→0017).
4. Seeds synthetic legacy rows (org → service → counter → tokens).
5. `alembic upgrade head` → 0018; asserts all five phase-9 tables exist and
   legacy rows survive.
6. FK audit via `information_schema.REFERENTIAL_CONSTRAINTS` and a
   named-index audit for the nine declared 0018 indexes.
7. `alembic downgrade 0017` — the MySQL **error 1553** regression check.
8. `alembic upgrade head` again — idempotence; FK set must be identical.
9. Drops the disposable DB in a `try/finally`, so teardown runs even when a
   chain step fails — but ONLY if this execution created it
   (`teardown_allowed(name, created_this_run)`); **every** `DROP` statement
   first passes `drop_disposable_check(target)` (exact allowlist) plus the
   ownership check — no exception path can drop an unrelated or
   pre-existing database.

Expected final line: `ALL-OK: real alembic chain 0001→0018 ...`.

## 6. Test execution and expected output

Job 1 (`pytest-suite`):

```
python -m pytest --tb=short --junitxml=pytest-results.xml
```

Normal fixtures only — `--noconftest` is intentionally **not** used. The
in-memory SQLite session fixture builds all ORM tables; no pytest case
connects to the MySQL service engine (pytest stays the authentic fixture
path; MySQL is exercised by the migration job).

Expected outputs:

- Step summary shows the count lines, e.g.
  `xxx passed, y skipped, z warnings` (857 collected on the Nov 6 hosted
  run: 857 passed, 715 warnings; investigate every failure — do not weaken
  tests to obtain green).
- Artifacts uploaded `always()`: `pytest-results.xml` (JUnit) and
  `pytest-output.txt` for failed-run post-mortems.

## 7. Investigating CI failures

1. Open the failed step; the `--tb=short` traceback names the failing test,
   expected vs actual.
2. Counts: compare passed/failed/skipped against the baseline recorded in
   `docs/PRODUCTION_READINESS_REPORT.md` (§5 ledger rows).
3. Classify the failure:
   - **Application defect** — fix the code; add/extend a regression test.
   - **Environment/infrastructure** — service not healthy, port conflict,
     image pull failure; check the service logs (`docker compose logs` uses
     the job's services section).
   - **Order/duration flake** — rerun once (`workflow_dispatch`) before
     blaming the code; genuine flakes get documented, never retried to green.
4. Distinguish *skipped-with-reason* (e.g. Redis unreachable if the service
   failed to start before the skip check ran) from *missing coverage* —
   a silently skipped integration test is not a PASS.

### 7.1 Hosted-run observations (Nov 6, real findings)

The first activation (commit `c0a3…`→`b628077`) surfaced these genuine
issues, all diagnosed **without repository-log access** (the logs and
artifact APIs require an admin token, which this environment does not
have — failures were surfaced through `::error::`/`::notice::` check-run
annotations read via the public check-runs API):

1. **razorpay + Python 3.12 (`ModuleNotFoundError: pkg_resources`)** —
   razorpay 1.4.2 imports `pkg_resources` at import time but does not
   declare setuptools; Python 3.12 venvs no longer bundle setuptools.
   Fixed with the pinned `setuptools==75.8.0` in `requirements.txt`
   (documented in-file). Any routine that imports `app.main` →
   `payment_service` hits this.
2. **GitHub Actions' default bash runs `bash -eo pipefail`** — a capture
   block like `pytest | tee f; code=${PIPESTATUS[0]}` is aborted by errexit
   before it runs. The `|| code=$?` pattern the workflow now uses is the
   errexit-safe way to capture and annotate failures.
3. **OS environment variables bypass `_env_file=None`** — the workflow's
   `DB_PASSWORD` job env made the "missing DB password in production"
   test see a value. Tests that exercise an *absence* of configuration
   must `monkeypatch.delenv(<VAR>, raising=False)` (see both tests in
   `tests/test_config_security.py` that do this).
4. **Unconfigured provider → `SUPPRESSED` outbox path** — with no `.env`
   on CI, `mail_username` is empty and outbox rows are written `SUPPRESSED`
   instead of `PENDING`. Tests that need a provider must configure it
   explicitly under `monkeypatch` (`tests/unit/test_notification_service.py`).
5. **FK audit is an exact declared-set comparison** — the former
   `>= 10 distinct (table, delete_rule) pairs` was unachievable by
   construction: 0018 declares 14 FKs which collapse to at most 9 distinct
   pairs. The audit now asserts equality against the 14 declared FKs
   (`DECLARED_FKS` in `scripts/ci_migration_chain_check.py`) — any missing,
   dropped, or unexpected FK is named in the failure message.

## 8. Reproducing CI results locally (authorized machines only)

On an IT-approved machine (see `docs/IT_BLOCKER_CODEINTEGRITY_0283ac0f.md` for
the exact policy blocker and approved-environment checklist):

```bash
# disposable MySQL 8.0.46
docker run -d --name skipline-ci-mysql \
  -e MYSQL_ROOT_PASSWORD=ci-only-synthetic-password \
  -e MYSQL_DATABASE=skipline_ci -p 3306:3306 mysql:8.0.46
docker run -d --name skipline-ci-redis -p 6379:6379 redis:7-alpine

# for the pytest suite (job 1)
export DB_HOST=127.0.0.1 DB_PORT=3306 DB_NAME=skipline_ci \
       DB_USER=root DB_PASSWORD=ci-only-synthetic-password \
       REDIS_TEST_URL=redis://127.0.0.1:6379/15

pip install -r requirements.txt
python -m pytest --tb=short

# for the migration checker (job 2): DB_NAME MUST be the exact allowlist
# value skipline_ci_migrate — the script refuses to run otherwise
export DB_NAME=skipline_ci_migrate
python scripts/ci_migration_chain_check.py

docker rm -f skipline-ci-mysql skipline-ci-redis
```

The current workstation cannot run the DB-backed portion (Code Integrity
blocks `_greenlet`) — do not weaken the policy; use an approved machine, the
CI runner, or WSL2 outside the managed policy according to IT guidance.
This workstation did still execute the **hermetic gate tests**
(`pytest tests/test_ci_migration_gate.py -v --noconftest`, 53/53 passed) and
the script's refusal/teardown paths, which do not import greenlet.

## 9. UI acceptance (six blocked flows) — required tooling

No browser automation exists in the repository today (no Playwright,
Selenium, Cypress, or traces of headless-browser fixtures). The six pending
flows are currently verified only at the HTTP-replay level plus committed API
suites; the acceptance contract requires real UI journeys **and will not be
claimed until they run**.

Smallest maintainable proposal (for future approval — intentionally not
implemented now):

- **Playwright** (Python) as a dev/test dependency, one file
  `tests/ui/test_phase9_acceptance_flows.py` driving the real served app
  (`uvicorn app.main:app`) against a disposable MySQL container with the
  synthetic seed from the prodcheck fixture fix (org workflows per §1.4),
  reusing the same counters/services/employee accounts created on the fly.
- One CI job variant `ui-acceptance` (job container + Playwright browsers)
  gated to run on demand first (`workflow_dispatch`), then on push after it
  stabilizes. Estimated surface: ~6 journeys × ~20 actions, well under the
  complexity of adding a full framework — justification for keeping it to
  Playwright's single driver instead of Selenium-Grid or Cypress.
- The kiosk flow evidence (§1.4) remains the recorded live PASS; the other
  six flows record PASS only when real browser journeys execute.

## 10. Security checklist for future CI edits

- Never add repository secrets for test credentials (synthetic values only).
- Keep `permissions: contents: read` as the job-level maximum; escalate only
  with an explicit, documented reason.
- Untrusted fork PRs must never gain secret access; the current design uses
  no secrets at all and must stay that way.
- Keep the migration script's **exact allowlist**
  (`ALLOWED_MIGRATION_DB = "skipline_ci_migrate"`, `ALLOWED_MIGRATION_HOSTS`
  loopback-only, `refuse_if_preexisting` before creation,
  `drop_disposable_check` + the `created_this_run` ownership flag before
  every `DROP`) intact when touching migration scripts; never let CI
  address, create over, or drop a database its run did not verifiably
  create. Any pre-existing `skipline_ci_migrate` on the target server must
  be removed by the operator — the script refuses and preserves it.
- No paid external calls (LLM, payment, mail, SMS) in CI.
- Workflow-file conventions (validated with `check-jsonschema` against
  GitHub's official `github-workflow.json` schema, Nov 6): quote the `"on":`
  key so YAML 1.1 parsers don't coerce it to boolean `True`; propagates exit
  codes through pipelines explicitly (`set -o pipefail` + `| tee`) so a
  failing pytest step can never be masked.
