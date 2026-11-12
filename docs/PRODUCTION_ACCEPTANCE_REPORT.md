# SkipLine — Production Acceptance Report (consolidated, evidence-based)

Consolidation date: **Nov 8, 2026 (tops at working tree on `f44c32009e0d6ece0c158d09736d4126a8613f81`)**
Scope: closure of the Production Readiness phase WITHOUT starting Phase 10.
Nothing in this report is committed; all session work is uncommitted on top of
`f44c320`.

---

## 1. Verdict

**B — VERIFICATION INCOMPLETE.**

Justification (honest, per requirements contract):

- ✅ Full automated suite **passed on a hosted runner** (857 passed) and the
  local full-suite rerun **also passed exit 0** (853 passed + 4
  Redis-integration self-skips: this workstation's `Application Control`
  policy intermittently blocks `_greenlet`).
- ✅ Real migration chain 0001→0018 green both locally (sync path, Nov 6–7
  evidence) and hosted (`mysql-migration-chain` job ALL-OK).
- ✅ Backup/restore mechanism executed **against live MySQL 8.0.46** —
  ALL-OK, first live run succeeded (run9, §8).
- ✅ Six AI-provider failure paths verified (§5) with deterministic mocks.
- ✅ Config production-guard suite extended and green (19/19).
- ❌ **Six Playwright UI acceptance flows: BLOCKED** on this workstation by
  the intermittent Application Control block of `_greenlet`` ` (Playwright
  imports greenlet; no bypass attempted). Harness is complete and reviewed —
  run evidence exists only for fixture-level checks, not full journeys.
- ❌ Docker deployment verification: BLOCKED (daemon unavailable).
- ❌ Live LLM provider call: NOT EXECUTED (no authorized `AI_*` credentials).
- ❌ SEC-009 legal review: outstanding **human approval** — cannot be marked
  complete by code inspection. RPO/RTO never fabricated.

Verdict is re-evaluated once §10 blocker 1–4 clear; no green-hacking.

---

## 2. Completed work this closure phase

| Work | Status | Evidence |
|---|---|---|
| Full 857-test hosted run re-confirmed green | DONE | run `42263779003` (`mk-abc-2014`). Also `42262780689` (session-8 record) |
| Full suite local re-run (before policy re-block) | DONE, exit 0 | `%TEMP%\opencode\prodcheck\full_run.log` + `full_junit.xml`; 853 passed + 4 `ssss` skips |
| Pydantic `from_attributes` warnings (7 schema classes) — fixed | DONE | `app/schemas/finance.py` `model_config = ConfigDict(from_attributes=True)` |
| 57 legacy `TemplateResponse(name,…)` deprecation warnings — fixed | DONE | routers `views_admin/employee/org/user`, `org.py`, `public.py`; 79 views tests re-passed |
| `ai_assistant_service` `utcnow` deprecation — fixed | DONE | `datetime.now(timezone.utc)` |
| Provider failure-path durability suite | DONE | `tests/unit/test_ai_provider_resilience.py` (6 passed): timeout / HTTP 429 / malformed / empty / **key-never-logged** |
| Six-flow UI acceptance harness + deterministic mock AI provider | DONE (harness); flows BLOCKED | `tests/acceptance/conftest.py` + `test_flow1…flow6`, `scripts/mock_ai_provider.py` (deployed strict-JSON action protocol, data-only `TOOL_RESULT` parsing, direct-`confirmed` injection impossible) |
| APP_DEBUG production hard-gate | DONE | `app/config.py` model validator errors when `APP_ENV=production` + `app_debug=True`; compose sets `APP_DEBUG=${APP_DEBUG:-false}`; `tests/test_config_security.py` → **19/19 passed** |
| Persistent file logging (compose `app_logs` volume use) | DONE | `LOG_DIR`/`LOG_FILE_LEVEL` settings + loguru rotating sink (formatting 20 MB × 5) in `app/main.py` lifespan |
| Compose MySQL engine pin | DONE | `mysql:8.0.46` (hosted-verified engine) for db + backup services |
| Backup/restore rehearsal script | DONE script (+ runs §8) | `scripts/backup_restore_rehearsal.py` — dump→restore→verify→owned-teardown, refuse-if-exists |
| Remaining-warning analysis (715 → categorized) | DONE | §6 (third-party causes listed, not "pending" forever, detailed below) |
| CI job (needed) for acceptance/ui job variant | PREPARED (not hosted-run authorized) | §10 next-action |
| Production acceptance report (this document) | DONE | §14-section |

---

## 3. Six Playwright browser acceptance flows

| # | Flow | Status | Notes |
|---|---|---|---|
| 1 | Organization service/workflow editor (HTML+JSON) | **BLOCKED** | collection-phase import error: playwright → `greenlet` DLL blocked by policy `0283ac0f` (log `acc_flow1e.log`) |
| 2 | Customer manual booking + owner-only tracking | **BLOCKED** | same environment blocker |
| 3 | Employee shift → stage advance/skip/recall lifecycle | **BLOCKED** | same |
| 4 | Kiosk walk-in + receipt (previously passed live Nov 5) | previously PASSED (live manual), new automation **BLOCKED** | same |
| 5 | Manual → AI handoff + confirmation gating | **BLOCKED** | same |
| 6 | AI → manual prefill + booking | **BLOCKED** | same |

All six record BLOCKED, NOT FAILED: the blocker is an **environment
capability** (import of `_greenlet`), not any failing assertion. Retry-ready
command below (§13). Prior session's fixture bugs are all fixed; only the
browser/import layer remains blocked.

---

## 4. Test totals (as executed, this phase)

| Suite / scope | When | Result |
|---|---|---|
| Hosted CI `pytest-suite` | run `42263779003` (mk-abc-2014 notebook re-confirmed) | **857 passed**, 715 warnings |
| Hosted CI `mysql-migration-chain` | same run | **ALL-OK** — up/down/re-up + exact 14 FK match + 9/9 indexes + legacy rows |
| Local full pytest (pre-reenablement) | this session | **853 passed + 4 Redis `ssss` skips, exit 0**, raw log saved |
| Provider-failure-path unit tests | this session | **6/6 passed** (file contains 6 tests; earlier "7/7" claims were an overcount — corrected Nov 8) |
| Config-security suite | this session | **19/19 passed** (added `test_prod_rejects_debug_true`) |
| CI migration gate (hermetic) | Nov 6–7 | **60/60 passed** |
| Acceptance browser flows | this session | **BLOCKED** (environment), see §3 |
| AI mock-provider continuity check (protocol integrity) | this session | py_compile OK; wired into 6-flow harness (see §3, BLOCKED) |

JUnit + raw logs preserved under `%TEMP%\opencode\prodcheck\` and will be
copied into the repository report appendix upon request.

---

## 5. AI status

- ✅ **Protocol-level**: strict-JSON action protocol compliance verified in
  Phase 9's 9/9 real-HTTP-transport case + this session's 6/6 resilience
  fixture.
- ✅ **Failure-mode durability**: timeout, 429, empty and malformed payloads -
  each returns a deterministic friendly error; **the API key NEVER appears in
  logs** (asserted).
- ✅ **Anti-privilege**: the LLM cannot produce `confirmed` (rigid endpoint
  `/ai/conversations/{id}/book`); deterministic mock enforces the same
  contract so UI-flow tests exercise the real path.
- ❌ **Live LLM provider call**: NOT EXECUTED. No `AI_*` credentials are
  authorized in this environment; a live short-call checklist remains
  queued (§10 blocker 1).
-その他: mock provider installed at `scripts/mock_ai_provider.py`
  (OpenAI-compatible; must never point at a real OpenAI account).

---

## 6. Warning analysis (verified from the actual logs, Nov 8)

Two evidence sources, kept distinct:

- **Hosted run (authoritative baseline):** 857 passed, **715 warnings** in
  52.99 s (run `42263779003`/`42262780689` annotations).
- **Local full run (Nov 7, `full_run.log`):** per-file grouped warning lines
  sum to **678** warnings across 37 grouped rows (top: `test_workflows` 98,
  `test_employees` 58, `test_ai_assistant` 50 — jose's `utcnow`
  deprecation fires at every signed-API call, which matches the wide
  distribution). Note: the saved log's console summary line was truncated
  by log capture; the authoritative local totals are
  `full_junit.xml`: **857 tests, 0 failures, 0 errors, 4 skipped
  (= 853 passed), 113.3 s**. The local run **predates some of the
  app-side warning fixes** described below (its details still show the
  pre-fix `app/services/ai_assistant_service.py:361` utcnow and one
  starlette legacy-call warning).

App-side fixes verified in the **current tree** (grep-verified this
session, not from the stale log):

| Cause | Fixed | Current-tree residual |
|---|---|---|
| `from_attributes` missing in finance schemas (7) | ✅ 7× `ConfigDict(from_attributes=True)` | 0 |
| legacy `TemplateResponse(name,…)` call sites | ✅ 57 across routers | **0 remaining** (grep) |
| `ai_assistant_service` `utcnow` | ✅ `datetime.now(timezone.utc)` | 0 remaining (grep) |
| class-based pydantic `Config:` in app/models/tests | — | **none found** (grep of `app/`, `models/`, `tests/`) |

Residual warnings are third-party, verified directly from the log's site-package paths:

| Third-party cause | Evidence path in log | Actionable? |
|---|---|---|
| `python-jose/jwt.py:311` `utcnow` deprecation (majority share — exercised on every JWT sign) | `jose\jwt.py:311` | upstream issue; jose is effectively unmaintained — replacement (`joserfc`) listed in OPEN_DECISIONS, deliberate future work |
| `razorpay/client.py:4` `pkg_resources` deprecation | `razorpay\client.py:4` | upstream; pinned setuptools mitigates removal risk |
| `reportlab/rl_safe_eval.py:11` `ast.NameConstant` (removal in 3.14) | `reportlab\lib\rl_safe_eval.py:11` | upstream; harmless until Py3.14 |
| `httpx/_client.py:1559` per-request cookies deprecation (test usage pattern) | `httpx\_client.py:1559` | test-side pattern; low priority |
| `pydantic/_internal/_config.py:284` `class-based config` PydanticDeprecatedSince20 (triggered from third-party code path; no app/model/test `class Config:` site exists) | `pydantic\_internal\_config.py:284` | origin not in app code; re-attribute on next run |
| `pydantic` config-security `RuntimeWarning`s (4) by design — the production-guard tests | `pydantic\main.py:176` | by design |

Exact post-fix totals require the **next authorized hosted run** (the local
rerun cannot be repeated while the greenlet block is active). None of the
remaining warnings is a production-blocking defect; none is suppressed.

---

## 7. Docker deployment verification

- BLOCKED: `docker info` fails (daemon unavailable on this workstation —
  documented earlier; absence of власть).
- Config-evidence review (the prepared change set un-committed):
  - `docker-compose.yml`: MySQL pinned `mysql:8.0.46`; `app_logs` volume
    mount + `LOG_DIR`/`LOG_FILE_LEVEL` wired; `APP_DEBUG` default to `false`;
    no compose-served secrets (**requires real `.env`**; compose parse fails
    when `DB_PASSWORD`/`MYSQL_ROOT_PASSWORD` missing — no default slip
    possible);
  - `.env.example` updated (documents both new settings).
- Reproduction readiness: `docker compose up --build -d` runbook exists
  (docs/deployment.md §8.rerun steps); the exact smoke list (health route,
  signed QR scan lifecycle, employee stage flow) is queued in §13.

---

## 8. Backup/restore rehearsal — ✅ VERIFIED (first live execution)

**Final result (run9, Nov 8, `%TEMP%\opencode\prodcheck\rehearsal_run9.log`):**

```
[5] BACKUP OK: skipline_bakrehearsal_20261108100159.sql.gz
    (114 725 B unpacked SQL; sha256=4383d41f…fac0278)
[6] artifact restored into scratch DB
[7] VERIFIED (schema_mode=sync-create_all): schema tables=65,
    alembic_version=0018, FKs=134, token/org/service rows=2
    (RH-0001@Rehearsal Clinic), users=1,
    workflow stages=[(registration,1), (consultation,2)]
[8] application-level access read on restored DB OK (waiting tokens=2)
[9] teardown: dropped only the DBs this run created (owned-cleanup pattern)
ALL-OK (schema_mode=sync-create_all): … verified in 32.1 s —
… No product/protected DB was touched.
```

- Mechanism verified end-to-end against live MySQL 8.0.46: real `mysqldump`
  with `scripts/backup.sh`-parity flags (`--single-transaction --routines
  --events --triggers --set-gtid-purged=OFF`) → gzip + sha256 + min-size →
  restore via the `mysql` client → full parity verification (tables, row
  set, FK count, alembic revision, representative join values) →
  application-level read on the restored DB → owned-only teardown.
- **Disclosure (degraded schema mode, banner-logged):** the source schema
  for run9 was built by the fallback `sync-create_all` mode (the project's
  own ORM metadata — 64 model tables — via synchronous PyMySQL,
  head-stamped `alembic_version=0018`) because the workstation policy again
  blocked alembic's async driver stack (`greenlet` DLL, policy `0283ac0f`).
  **No policy bypass was attempted.** The backup→restore→verify pipeline —
  the operational requirement — is fully verified; a repeat run through the
  REAL alembic migration chain takes precedence and remains queued for a
  policy-open window (the chain itself is independently verified green on
  disposable MySQL, §5 rows 30/37 of the readiness report).
- Script-own defects found and fixed during rehearsal hardening (rehearsal
  harness defects, not product defects): engines/connections closed and
  disposed before teardown DROPs (an unclosed `Connection` held table MDLs
  and deadlocked run7), `backups_dir` NameError (run8), refuse-if-exists +
  exact schema-mode banners. Runs 1–8 were honest failures, all logged.
- Safety: only this run's `skipline_bakrehearsal_20261108100159` /
  `skipline_bakrestore_20261108100159` DBs existed during the run;
  protected DBs (`skipline`, `skipline_phase9_prodcheck`,
  `skipline_synccheck`) printed untouched in every run's `[1]` line;
  post-run orphan check: **none**.

---

## 9. Security findings (incl. secret scan — fully verified)

### 9.1 Secret scan — scope and coverage (user-requested verification)

Patterns searched across **all tracked, untracked, generated and CI files
(2106 text files + one 11.4 MB zip rendered entry-wise)**: private-key PEM
markers (`-----BEGIN … PRIVATE KEY`), AWS `AKIA…`, OpenAI `sk-…`, Stripe
`sk_live/test_`, Razorpay `rzp_live/test_`, Google `AIza…`, GitHub
`ghp/ghs/gho/ghr_`, Slack `xox…`, Twilio `AC…32hex`, URLs that embed
credentials (`scheme://user:pass@host`), and every `KEY=value` / YAML
`KEY: value` literal whose key names `PASSWORD|SECRET|API_KEY|TOKEN|CREDENTIAL`
(mu115 variant). **No result was suppressed.**

### 9.2 Classification of every non-clean match

| File:Line | Match | Classification | Verdict |
|---|---|---|---|
| `.env:13,18` | `SECRET_KEY`/`JWT_SECRET_KEY = change-me-…` | **PLACEHOLDER** (documented pattern; counted in `app/config.py` `_INSECURE_PLACEHOLDER` list) | benign (local dev only) |
| `.env:28` | `DB_PASSWORD` (32 chars, no synthetic-token match) | **ACTUAL CREDENTIAL — local MySQL root** | real but git-ignored, host-local only; rotate-per-§6.6 policy applies |
| `.env:27,26` | `DB_USER=root`, `DB_NAME=skipline` | ordinary local config (not secrets) | benign |
| `.env:64` | `ADMIN_PASSWORD` (25 chars, no synthetic-token match) | **ACTUAL CREDENTIAL — local dev admin** | real but git-ignored, host-local only |
| `.env:34` | `MAIL_PASSWORD = your-app-pas…` | **PLACEHOLDER** | benign |
| `.env:43` | `TWILIO_ACCOUNT_SID = ACxxxxxx…` | **PLACEHOLDER (dummy)** | benign |
| `.env:44` | `TWILIO_AUTH_TOKEN = your-twilio-…` | **PLACEHOLDER** | benign |
| `.env:48` | `RAZORPAY_KEY_ID = rzp_te…` (test-mode prefix) | **SYNTHETIC (test-mode ID)** | benign |
| `.env:49,51` | `your-razorpay…` / `your-webhook…` | **PLACEHOLDER** | benign |
| `.env:30` | `# DATABASE_URL=…` | **commented out** | benign |
| `.env.example` | all `<set-a-…>` markers; `AI_API_KEY` commented | **PLACEHOLDER** | benign (tracked intentionally) |
| `docker-compose.yml:15,18,25,136` | `${VAR:?required}` | **PLACEHOLDER-INTERPOLATION** (fails compose parse rather than defaulting) | benign ✅ safety design |
| `.github/workflows/production-readiness-tests.yml:53,79,168,181` | `ci-only-synthetic-password` | **SYNTHETIC test value** | benign ✅ |
| `tests/test_config_security.py` (~60 lines 52–273) | `Prodcheck-…`, `pk-…`, `<set-…>`-markers, `dev-db-pw`, `short-pw`, `right-<mixed>-angles-88` | **SYNTHETIC test constants** (present precisely because production validators must reject their weak variants) | benign ✅ |
| `tests/acceptance/conftest.py:51–52` | `AcceptancePass#2026`, `EmpAcceptance#2026` | **SYNTHETIC materialized fixture passwords** (header comment says NOT production credentials) | benign ✅ |
| `tests/acceptance/conftest.py:110–113` | `org_password: str` etc. | **ordinary code** (dataclass field declarations) | benign |
| `tests/unit/test_ai_provider_resilience.py` | (scan clean) | — | benign |
| `scripts/backup_restore_rehearsal.py:108` | `f"…{pw}@{host}"` | **ordinary code** (URL built from env vars; no literal) | benign |
| `scripts/mock_ai_provider.py`, `scripts/ci_migration_chain_check.py` | (scan clean; env-driven) | — | benign |
| `alembic.ini:5` | `driver://user:pass@localhost/dbname` | **alembic's stock sample URL**, never used (runtime URL constructed from env vars in `migrations/env.py`) | benign |
| `app/websocket/bus.py:72`, `docs/OPEN_DECISIONS.md:248` | docstrings mentioning credential-redaction behavior | ordinary code/documentation | benign |
| `templates/org/payments.html:433` | JS template-literal idempotency key | ordinary code | benign |
| `skipline_backend.zip` (11.4 MB, untracked; ignored via `.gitignore:75 *.zip`) | 3464 entries: **vendored third-party packages** (cryptography/pip fixtures); `-----BEGIN PRIVATE KEY` matches are library test-vectors, URL-cred matches are vendor code | **generated third-party artifact**; zero project `.env`-like entries | benign but **removal recommended** (some scanners flag it; it is not project code) |
| `backups/skipline_bakrehearsal_*.sql.gz` (3 dumps) | decompressed content scanned: **0** bcrypt-hash / assignment / URL-cred / provider-key matches; contains only synthetic rehearsal rows | **generated local artifacts**, git-invisible (backups/.gitignore `*` except itself) | benign ✅ |
| Colon-style scan total 146 hits incl. code | — | all ordinary code (pydantic field names, function params, dict keys, model columns) | benign |

### 9.3 Git-tracked-state guarantees (verified, not asserted)

- `.env` **untracked and ignored**: `.gitignore:46`, `git check-ignore -v
  .env` → `.gitignore:46:.env`; `git log --all -- .env` → **no commit ever
  touched it**.
- Only tracked files matching secret-ish names: `.env.example` (placeholders)
  and `templates/admin/change_password.html` (UI form) — both benign.
- Hosted CI workflow references **no repo secrets** at all.
- Session-8-era commit `c0a30e59f9` had a clean pre-push secret scan
  (recorded); the earlier hosted runs contain only synthetic CI values.

### 9.4 Synthetic-credential → production leakage guards (verified)

- `docker-compose.yml` hard-fails without `DB_PASSWORD`/`MYSQL_ROOT_PASSWORD`
  (`${VAR:?…}` — no defaults).
- `app/config.py` production guards: `_INSECURE_PLACEHOLDER` list +
  `_is_unfilled_marker()` + 32-char minimum severity for global secrets;
  production `ADMIN_PASSWORD` rejects unfilled markers; `APP_DEBUG=true` in
  `APP_ENV=production` now hard-errors.
- Test suites that install synthetic values run only under test fixtures /
  monkeypatch via `os.environ.setdefault`; the "elastic materialized
  credentials" live in `tests/`, are never referenced by `app/` code, and
  deployment docs require `secrets.token_urlsafe`-generated values.

### 9.5 Remaining risks (honest)

1. **Real local credentials exist in git-ignored `.env`** (MySQL root + dev
   admin): a machine compromise or transcript leak exposes them — rotate
   local dev secrets if the transcript leaves the team (pre-existing
   blocker §6.6 of the readiness report still stands).
2. **Strong-but-synthetic constants (e.g., `Prodcheck-DB-2026-strong`) pass
   production-strength validators** — validators check strength, not
   provenance; the operator's `.env`-authoring procedure (readiness report
   §8) is the control. Recorded, not suppressed.
3. **`skipline_backend.zip`** is a third-party bundle retained on disk;
   benign classification above is evidence-based, but it should be removed
   from the working copy eventually to avoid future confusion.
4. Pattern-based scan: a secret in an *unusual format* could evade the
   pattern set; scans were run with two independent grammars (`=` and `:`
   assignment shapes) and the highest-signal provider keys. A full
   **git-content-history** scan was not performed (scope: tracked/untracked/
   generated/CI working tree + the known real-credential file's history).
5. `backups/` directory contents are synthetic today but will contain real
   data once production rests there; the directory's inner `.gitignore`
   (`*` except itself) must stay ** intact — operators should preserve it
   when adjusting backups policy.

**Conclusion**: zero real credentials are tracked or scan-visible outside
the local git-ignored `.env`; production configuration cannot accept
synthetic/test values by design; all scan findings were individually
classified above.

---

## 10. Remaining blockers — final closure matrix (Nov 8)

| # | Item | Status | Evidence already available | Exact missing evidence | Why not completable now | Required env / authorization | Next exact execution |
|---|---|---|---|---|---|---|---|
| 1 | Six Playwright UI acceptance flows | **BLOCKED** | Harness complete (7 tests / 6 files, §3); per-flow coverage table (below, §10.1); last execution attempt this session: `pytest --collect-only` → 6 collection ImportErrors (`playwright`→`greenlet` DLL, policy `0283ac0f`). **Container evidence (Nov 8):** on the ARM64 Linux engine (WDAC does not apply) the same 6 modules still fail collection — `ModuleNotFoundError: No module named 'playwright'`: **`playwright` is not pinned in `requirements.txt`** (image and workflow both install only `-r requirements.txt`) | An actual green run with pass/fail counts per flow | Workstation App Control blocks `_greenlet` in the venv; in the container the package is simply absent from the pinned dependency set. No bypass attempted | (a) IT remediation window per `docs/IT_BLOCKER_CODEINTEGRITY_0283ac0f.md`; (b) add `playwright` (+a browsers/Image variant) to the pinned deps and run a container job; (c) hosted Linux runner job | One of: workstation `python -m pytest tests/acceptance -x -q --tb=long` once greenlet loads — or a `ui-acceptance` job (hosted runner or Docker ARM64 container with `playwright install --with-deps chromium`, pointing at the harness URL/port) |
| 2 | Docker build + bring-up + smoke | **QUALIFIED PASS (probe-mode subset, Nov 8)** | ARM64-native build of `skip-line-app` exit 0 (exact pinned reqs, aarch64 wheels); probe redis 7.4.12 PASS; disposable MySQL 8.0.46 (aarch64) healthy; **real alembic chain ALL-OK exit 0 in-container** (14/14 FKs, 9/9 indexes, no 1553); app bring-up via `start.sh` → `/health` = `{"status":"ok","app":"Skip Line"}`, probe `alembic_version=0018`; `.env`-in-image leak found and fixed (`.dockerignore`, rebuild, `/app/.env` verified absent) | Full compose bring-up with published ports (nginx 80/443, db 3306) — host MySQL owns 3306 locally, so the full profile is exercised on a runner/CI, not on this workstation | Local port conflicts; clamav profile is amd64-only + optional; not forced (rule: no blind platform forcing) | Any clean host or hosted CI | `docker compose config` → `docker compose up --build -d` → `docker compose ps` (health) → `curl /health` → restart persistence → `docker compose down` (see `docs/deployment.md`) |
| 3 | Backup/restore via the REAL alembic chain (not the fallback) | **QUALIFIED FALLBACK PASS (run9)** | run9 `ALL-OK`, exit 0, 32.1 s — dump/restore/verify/teardown fully verified with the banner-disclosed `sync-create_all` schema mode; real chain itself verified green separately (readiness §5 rows 30/37) **and now additionally in-container on ARM64 (ALL-OK exit 0 via `ci_migration_chain_check.py` — corroboration, NOT a rehearsal substitute)** | One rehearsal run whose `[3]` line reads `(real alembic chain)` | The rehearsal script itself has not been executed with the async `env.py` path (greenlet) on any available environment yet | IT remediation window or any runner with working greenlet | `python scripts/backup_restore_rehearsal.py` (identical; asserts the head-stamp either way; never repeats the fallback as alembic evidence) |
| 4 | Live AI provider call | **BLOCKED** | Deterministic mock provider 9/9 transport + 6/6 resilience (timeout/429/malformed/empty/key-never-logged); protocol safety (no LLM booking authority) verified | One live round-trip incl. one imported-then-refused confirmation asserting no booking + no key in logs | No `AI_*` credentials exist in `.env` and none may be requested/self-provisioned | Written authorization + funded metered provider account (operator-provided) | After credentials are operator-provisioned: set `AI_PROVIDER`/`AI_API_KEY` in a scratch env, one message → verify draft → trigger `#confirmBar` refusal round-trip → `grep -c "$AI_API_KEY" app_logs/*` = 0 |
| 5 | SEC-009 legal review | **PENDING HUMAN** | Full handover prepared: `docs/SEC009_LEGAL_HANDOVER.md` (requirement text, controls, privacy surface, 6 unresolved questions, evidence refs, signed-approval block) | The signed approval block | Cannot be completed by code or the verifier | Authorized human reviewer (legal/privacy) | Reviewer completes §5 approval block in the handover document |
| 6 | Hosted CI re-run confirming post-fix warning counts + final suite | **PENDING AUTHORIZATION** | Recorded greens: run `42263779003` (857 passed/715 warnings) re-confirmed this closure; `origin/main` == HEAD == `f44c320` verified this session | One run of the existing workflow on the current HEAD + uncommitted changeset pushed | Push and workflow execution require user authorization (verifier never triggers runs) | User | `git commit` of the changeset (user-side), then GitHub → run workflow (or `gh run create`) |
| 7 | Full pytest suite rerun incl. provider-resilience + config 19/19 in one log | **QUALIFIED PASS (in-container, ARM64 Linux)** | Consolidated post-fix suite executed inside the ARM64 container: junit **tests=864 failures=0 errors=0 skipped=4 (real-Redis integration), exit 0, 63.6 s** — includes resilience 6/6 + config 19/19 + gate 60/60 in one log (`co/container_ct_junit.xml`); local full-suite rerun on the workstation venv remains blocked by WDAC | Hosted-CI rerun of the same consolidated suite (blocker #6) | Host venv greenlet blocked; container path was used instead (see `docs/ARCHITECTURE_COMPATIBILITY_REPORT.md` §9) | Workstation+IT, or hosted runner | hosted: workflow run after user-side commit; local: `python -m pytest --tb=short --junitxml=full_junit2.xml` on a policy-open window |

Verdict remains **B — VERIFICATION INCOMPLETE** (§1): no verified critical
defect exists, but items #1–#7 block mandatory acceptance or approval
completion. Verdict **A** is unreachable until #1–#5 are green and #5 is
signed; a verified defect would drop the verdict to **C** (none found).

### 10.1 Per-flow coverage table (from the actual test code, inspected Nov 8)

| Flow | Test (file::name) | Browser UI? | Real backend + isolated DB? | Authorization / persistence / tenant-isolation / result-state assertions | Latest execution | Gaps |
|---|---|---|---|---|---|---|
| 1 Org workflow config | `test_flow1_org_workflows.py::test_flow1_configure_and_persist_workflow` + `::test_flow1_unauthorized_cannot_modify_workflows` | ✅ login form + `#wfService`/stage-row builder + `Create Workflow` click | ✅ uvicorn + disposable `skipline_acceptance_*` DB | Authz: customer POST → 401/403, DB unchanged; persistence: workflow row + exact 3-stage config re-verified in DB; result state ✅ | BLOCKED (collection ImportError) | none substantive |
| 2 Booking & tracking | `test_flow2_booking_tracking.py::test_flow2_book_track_and_track_owner_isolation` | ✅ `#bookModal` selects + `#confirmBookBtn`, tracking page + receipt navigation | ✅ same | Result: token DB row (owner/counter/service/queue pos) + waiting state; tenancy: user2 gets "Token not found" on track AND receipt; owner-only pages | BLOCKED | none substantive |
| 3 Employee stages & recall | `test_flow3_employee_stages.py::test_flow3_stage_progression_mandatory_skip_refusal_and_recall` | ✅ shift form → org decision modal → clock-in → `Call Next` → stage action buttons → `prompt()` recall dialogs | ✅ same | Authz: pre-shift stage op → ≥400; server-side validation: mandatory skip → exactly 400 + DB state unchanged; persistence: append-only `token_stage_logs` audit incl. recall reason; result state: current stage = verification | BLOCKED | none substantive |
| 4 Kiosk walk-in | `test_flow4_kiosk_walkin_issue_track_and_idempotency` (`test_flow4_kiosk.py`) | ✅ `/user/kiosk/{id}` service→counter→guest details→issue | ✅ same | Result: `booking_source='kiosk'`, guest fields, counter binding, `user_id IS NULL`; idempotency: duplicate `client_reference` replay → same id, exactly one new booking | BLOCKED | receipt/track links asserted by HTTP status (soft) rather than full page render — minor |
| 5 Manual→AI continuity | `test_flow5_manual_book_then_ai_continuity_confirm_gating` (`test_flow5_manual_to_ai.py`) | ✅ `#continueWithAi` handoff into `/user/assistant`, real chat input to deterministic mock over REAL backend tools | ✅ same | Anti-invention: draft text must contain the exact seeded org/service/price 49.00; gating: token count unchanged pre-confirm; idempotency: repeat confirm → no new booking; tenancy: user2 → uniform 404 | BLOCKED | repeat-confirm assert shape is loose (`in (True, False)`) but the token-count assertion is the substantive gate |
| 6 AI→manual continuity | `test_flow6_ai_draft_toolverifying_context_and_manual_completion` (`test_flow6_ai_to_manual.py`) | ✅ chat → draft → `Switch to Manual` → `#manualPrefill` → real booking modal | ✅ same | Context carryover: `#manualPrefill` + conversation-state draft ids must equal the manual booking (org/service/counter); result state: exactly one new token, waiting, right owner; tenancy: user2 `404` | BLOCKED | none substantive |

All six use the real served app (`uvicorn` subprocess over the seeded
disposable MySQL) — no mocked frontend, no fixture-only substitutes.
Because no browser journey has executed yet, **no flow is claimed PASSED**;
the per-flow table is a code-level completeness review, not execution
evidence.

---

## 11. Documentation updates made this phase

- `docs/PRODUCTION_READINESS_REPORT.md` — ledger rows 38–42 appended;
  FINAL-STATUS addendum + remaining-blocker matrix cross-reference added.
- `docs/CI_VERIFICATION_GUIDE.md` — §9 replaced with the **actually
  implemented** Playwright harness description + run instructions.
- `docs/PRODUCTION_ACCEPTANCE_REPORT.md` — this file (created; §6 evidence-
  corrected, §10 = remaining-blocker matrix + per-flow coverage review).
- `docs/SEC009_LEGAL_HANDOVER.md` — created (SEC-009 human-review handover).
- `docs/IT_BLOCKER_CODEINTEGRITY_0283ac0f.md` — unchanged (still accurate).

---

## 12. Git status (as of consolidation)

- HEAD = `f44c32009e0d6ece0c158d09736d4126a8613f81` (unchanged all session;
  NOT pushed).
- 13 modified files (warning fixes, config hard-gate, compose, docs),
  4 new files (`scripts/backup_restore_rehearsal.py`,
  `scripts/mock_ai_provider.py`, `tests/unit/test_ai_provider_resilience.py`,
  + the `tests/acceptance/` directory = 7 files), **uncommitted**.

Diffstat summary ~147 insertions / 94 deletions + new files; full inventory
available on request.

---

## 13. Reproduction commands

```text
# full suite on an approved machine (greenlet available)
python -m pytest --tb=short

# six acceptance flows (needs greenlet + Chromium)
python -m pytest tests/acceptance -x -q

# backup/restore rehearsal against live local MySQL (disposable DBs only)
export DB_HOST=… DB_PORT=… DB_USER=… DB_PASSWORD=…
python scripts/backup_restore_rehearsal.py

# hosted CI re-run (requires your authorization)
gh run create --workflow production-readiness-tests.yml -R Arunkumar-codec/skip-line
```

All disposal artifacts (disposable DBs) are named uniquely and torn down
owned-only; protected DBs are read-never-by-default (script refuses overlap).

---

## 14. Next single action

The verifier-side closure items are complete (backup/restore rehearsal
`ALL-OK` §8; secret scan classified §9; docs updated §11, ledger rows 38–42
appended). The single next action is **human**: authorize one of —

1. an IT fix/whitelist window for the intermittent `_greenlet`
   Application Control block (then: `python -m pytest tests/acceptance -x
   -q` for the six flows + one full-suite rerun), or
2. a hosted CI run of the prepared workflow (including a `ui-acceptance`
   `workflow_dispatch` variant) on GitHub-hosted Linux, which has already
   executed this suite green without the policy blocker.

Everything else waits on the §10 human blockers (Docker daemon, live-LLM
authorization, SEC-009 legal review, commit/push authorization).

---

## 15. WP1 addendum (Nov 12, 2026) — Playwright flows CONVERGED; real-scripts rehearsal PASS

Working tree of `main` @ `f44c32009e0d6ece0c158d09736d4126a8613f81` (unchanged,
all WP1 work uncommitted, 0 staged). Canonical artifacts: junit
`%TEMP%\opencode\wp1_acceptance_junit.xml` (run 13) and
`%LOCALAPPDATA%\Temp\opencode\wp1\rehearsal_evidence.json`.

### 15.1 Acceptance suite — 7/7 PASS (run 13 of 13)

Run 13 junit: **tests=7 failures=0 errors=0 skipped=0, exit 0, 118 s**.
Convergence history (one root cause at a time): runs 7–8 (2 passed) → 9
(3) → 10 (5) → 11 (6) → 12 (6) → **13 (7/7)**. Fixes beyond §3:

- **3 genuine APP defects found and fixed by the flows** (authorized under
  Task 2 "fix genuine APP failures revealed by tests"):
  1. `app/routers/tokens.py` `download_receipt` — `MissingGreenlet` on the
     lazy relationship; replaced with explicit `db.get(Branch, …)`.
  2. `app/services/schedule_service.py` `get_public_slots_for_date` — the
     404 guard wrongly treated `queue_mode != SLOTS` as an error, breaking
     the documented legacy day-queue contract (`is_legacy_day_queue`);
     the downstream slots query already filters SLOTS.
  3. `app/routers/employee.py` `get_counter_queue_api` — referenced
     nonexistent `TokenStatus.SERVING` → AttributeError → unhandled 500 on
     **every** operator queue poll (the dashboard swallowed it and stuck at
     "Loading queue…"). Fixed to `WAITING/CALLED/IN_PROGRESS/ON_HOLD`
     (same active-set as `user_service.py`).
- **Test-harness fixes** (each citing run evidence in comments): PDF
  receipts verified via `page.context.request.get` (200 + `application/pdf`
  owner; uniform 404 cross-user — `page.goto` on a PDF raises "Download is
  starting"); flow3 FIFO queue isolation + positive serving-token assertion
  (the em-dash placeholder defeated `not_to_have_text("-")`) + real button
  label "Next Stage" + async dialog handler (`Dialog.accept()` is a
  coroutine in the async API); conftest emails `@acceptance.dev` (pydantic
  v2 `EmailStr` rejects the RFC-reserved `.test` → every JSON login was a
  guaranteed 422); httpx 0.27 cookie mapping from `cookies.jar`;
  flow6 captures `conversation_id` Python-side (JS global gone after
  navigation).
- Harness infrastructure change: the conftest `server` fixture is a SYNC
  session fixture running the ASGI app on a private event loop on a daemon
  thread (`run_coroutine_threadsafe` helpers) — this permanently killed the
  earlier event-loop-mismatch error class and removed the greenlet/WDAC
  dependency of the old async-fixture design.

### 15.2 Backup/restore rehearsal — PASS via the REAL production scripts

`scripts/wps1_backup_restore_rehearsal.py` (Task 3 driver; production
`scripts/backup.sh`/`restore.sh` **not modified**) — evidence
`rehearsal_evidence.json`, stamp `20261112152349`, **status PASS**:

1. Disposable `skipline_wps_backup_<stamp>` created (protected-DB guard).
2. **Real alembic chain 0001→0018** (15.2 s, async `env.py` path) — this is
   the "real chain" run §10 blocker 3 required.
3. Representative seed: org APPROVED, service, counter, tokens **RH-0001 /
   RH-0002**, 2-stage workflow (registration, consultation).
4. **Real `scripts/backup.sh`** (2.6 s): artifact
   `skipline_wps_backup_<stamp>_20261112T095406Z.sql.gz`, 12,214 bytes,
   sha256 `6ebe06c4…7b64`.
5. **Real `scripts/restore.sh`** into `skipline_wps_restore_<stamp>`
   (3.4 s, scratch-validation mode).
6. **Parity 6/6 MATCH**: 65 tables, 230 indexes, 134 FKs,
   `alembic_version=0018`, token rows, waiting tokens — backup DB vs
   restored DB.
7. **App-level read on the restored DB**: real uvicorn boot → `/health`
   200; async-ORM read = 2 WAITING tokens, both workflow stages,
   `alembic_version=0018`.
8. Owned teardown dropped both `skipline_wps_*` DBs; protected names
   (`skipline`, `skipline_phase9_prodcheck`, `skipline_synccheck`) are
   refused by guard and were never touched.

Driver-side environment fixes (all in the WP1 driver, none in production
scripts): pymysql cursor-based execution; autoincrement PKs never
synthesized (explicit PK insert doesn't update `LAST_INSERT_ID()` → child
FK break); length-aware varchar synthesis; 0015-reconciled UPPERCASE enum
seed values; explicit workflow-stage `code` (unique constraint); CRLF
`.env` sourced via `tr -d '\r'` temp copy (`.env` never edited); WSL
interop shim (`mysqldump`/`mysql` shims exec the real Windows MySQL 8.0
binaries — bare names don't resolve via interop). This closes §10 blocker
3 to **PASS (real chain + real scripts)**.

### 15.3 Verdict impact

The §1 justification item "❌ Six Playwright UI acceptance flows: BLOCKED"
and blocker #1 are now **resolved on this workstation** (no WDAC bypass —
the sync-fixture design simply doesn't import greenlet at collection in
the blocking path). §10 blocker #3 rehearsal is now a real-chain,
real-scripts PASS. Verdict B items remaining: #2 (full compose bring-up),
#4 (live LLM call — needs operator credentials), #5 (SEC-009 signature),
#6 (hosted CI rerun on the pushed changeset).


---

## 15. Final Production Acceptance Gate (Nov 12, 2026 — post WP1/WP2/WP3)

Assessed on baseline HEAD `4af7f96020504e57e71d2c5864b82c4be7379dd0`
(WP1 `c2fa863`, WP2 `9872cd5`, WP3 `4af7f96` — all committed and pushed;
HEAD == `origin/main`; working tree clean at gate start). This section is
append-only; nothing above §14 was altered.

### 15.1 Technical & Security Readiness — COMPLETE

| Gate | Status | Evidence |
|---|---|---|
| Acceptance flows (UI, end-to-end) | **7/7 PASS** (220.0 s) under the WP3-rotated `JWT_SECRET_KEY`/`SECRET_KEY` — flows 1–6 incl. kiosk, manual→AI, AI→manual | `%TEMP%\opencode\final_acceptance_junit.xml` (Nov 12) |
| Full regression (non-acceptance) | **915 tests / 0 failures / 0 errors / 4 real-Redis skips, exit 0** (WP3, Row 49) | `%TEMP%\opencode\wp3_regression_junit.xml` |
| Least-privilege DB runtime | `skipline_app`@`localhost`, zero global/banned privileges; full alembic 0001→0018 under the app identity; root rotated, old credential dead | Row 48; `wps2_evidence.json` / `wps2_privcheck_evidence.json` |
| Application secrets | `SECRET_KEY`/`JWT_SECRET_KEY` (64-ch, distinct) + `ADMIN_PASSWORD` (32-ch) rotated; live seeded admin hash rotated too (old dev password dead); production-mode validation clean | Row 49; `wp3_secret_rotation.json` |
| Optional-provider degradation | Razorpay unconfigured → clean 503; SMTP/outbox → honest SUPPRESSED; AI → `AiProviderUnavailable` fallback; Twilio = documented placeholder | Row 49; `test_optional_provider_gates.py` 27/27 |
| Residual credential leaks | 0 credential-shaped values in 360 artifacts; old root + dev admin passwords unrecoverable | Row 48; `wps2_leak_redaction.json` |

### 15.2 Infrastructure gates

- **Docker Compose full stack:** `BLOCKED (Docker Desktop engine offline)`
  — engine named pipe absent (`docker version` fails at the server step).
  No Docker state was modified. Compose hardening (required-variable
  guards, no hardcoded passwords, least-privilege identity, no `.env` in
  image) is already committed and unit-locked; the first containerized
  bring-up remains the owner's gate (note the first-init-only MySQL volume
  caveat from the root-password origin report).
- **Hosted CI on `4af7f96`:** not verifiable from this workstation — the
  GitHub CLI is not installed and the repository is private (unauthenticated
  API returns 404). **Owner instructions:** open the repo's **Actions** tab
  on github.com for the `4af7f96` commit, or `gh auth login` then
  `gh run list --commit 4af7f96`. Prior hosted runs of this suite are green
  (Row 43/44 evidence).

### 15.3 Production configuration & provider posture (values masked)

`.env` identity/secrets — all verified set & strong (never printed):
`DB_USER=skipline_app`; `DB_PASSWORD` 40-ch; `MYSQL_ROOT_PASSWORD` 40-ch;
`SECRET_KEY` 64-ch; `JWT_SECRET_KEY` 64-ch (distinct); `ADMIN_PASSWORD`
32-ch. Production-mode validation: clean (zero warnings/errors).

| Integration | Live state | Degradation behavior |
|---|---|---|
| **AI provider** | INTENTIONALLY DISABLED (no `AI_*` keys in `.env`) | `AiProviderUnavailable` → assistant falls back cleanly; core booking unaffected (tested) |
| **Razorpay** | Keys present in provider-plausible format; per the Nov 8 origin audit these are **placeholders** (no provider ever contacted by the app). NOT verified live (no-external-calls rule). | WP3 503 guard engages only when keys are EMPTY — with placeholders an initiate attempt would reach the SDK. **Owner decision:** fill live keys or blank the placeholders (blank ⇒ clean 503). |
| **SMTP / outbox** | `MAIL_*` present in plausible format; per origin audit = placeholder. | Unconfigured channels → `SUPPRESSED ("provider unconfigured")`, never retried; failing-configured capped at `max_attempts` with 30/60/120 s backoff. |
| **Twilio SMS** | `TWILIO_*` present in plausible format; per origin audit = placeholder; implementation is a **documented placeholder — not wired end-to-end** | `send_sms` always returns honestly False; absence blocks nothing (SUPPRESSED rows). |

No provider placeholder causes startup warnings or errors (verified in
production-mode validation).

### 15.4 SEC-009 legal handover & DB hygiene

- `docs/SEC009_LEGAL_HANDOVER.md` verified accurate and complete:
  technical controls (audit logging, KYC/verification workflow,
  data-minimisation primitives, payment integrity, secret-scanning) are all
  referenced with evidence locations.
- **Human-only open items:** written answers to §3 questions 1–6 (identity
  retention, guest consent, receipt/tax statements, AI conversation
  retention, listing pre-approval, data residency) + the signed §5 approval
  block (reviewer name, role, date, verdict).
- **Leftover database:** `skipline_acceptance_20261109104208_54003e`
  (Nov 9, 65 tables) remains on host MySQL — **drop-or-retain decision
  reserved to the owner; not touched.** Today's runs tore down owned-only
  (verified post-suite: no 2026-11-12 leftovers).

### 15.5 Verdict & the human sign-off checklist for Verdict A

**Technical & Security Readiness: COMPLETE** (WP1 + WP2 + WP3 verified;
7/7 acceptance + 915-test regression under the current secrets).

**Verdict A (Production Ready) is blocked ONLY by these human sign-offs:**

1. [ ] **SEC-009 legal signature** — `docs/SEC009_LEGAL_HANDOVER.md` §5
   approval block completed by the authorized reviewer (written answers to
   §3 required).
2. [ ] **Docker Desktop bring-up** — start Docker Desktop, then:
   `docker compose config --quiet` → `docker compose up -d --build` →
   verify `GET http://localhost:8000/health` = 200 and `/app/.env` absent
   in the app container → `docker compose down` (keep volumes).
3. [ ] **Hosted CI rerun** — confirm GitHub Actions green on `4af7f96`
   (Actions tab or `gh run list --commit 4af7f96`).
4. [ ] **Optional-provider key decision** — for Razorpay/SMTP/Twilio:
   fill LIVE credentials (then re-run the payment/SMS/email evidence) OR
   blank the placeholder values so the clean 503/SUPPRESSED paths engage.
5. [ ] **Leftover DB disposal** — drop or retain
   `skipline_acceptance_20261109104208_54003e` (owner's call).
6. [ ] **Final authorization to start Phase 10** — explicit owner approval
   (Phase 10 remains ON HOLD until then; no Phase 10 code was written in
   this session).

When items 1–5 are checked, this gate can be marked **Verdict A** and item
6 unlocks Phase 10. All doc updates in this section are uncommitted for
final owner review.
