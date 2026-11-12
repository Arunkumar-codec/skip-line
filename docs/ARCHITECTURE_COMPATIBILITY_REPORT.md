# SkipLine — Architecture Compatibility Report (Windows on ARM)

Date: Nov 8, 2026 · Verifier session: architecture & environment remediation
Baseline: HEAD/`origin/main` = `f44c32009e0d6ece0c158d09736d4126a8613f81`
(uncommitted closure changeset preserved untouched).

---

## 1. Hardware and Windows architecture

| Property | Value (evidence) |
|---|---|
| Processor | Snapdragon(R) X - X126100 - Qualcomm(R) Oryon(TM) CPU, 8 cores (`Win32_Processor.Name`, `Architecture=12`) |
| Windows product | "Windows 10 Home Single Language", WindowsVersion 2009, build **26200** (`Get-ComputerInfo`) |
| Windows architecture | **ARM 64-bit** (`OsArchitecture`) |
| AMD64 (x64) emulation | **AVAILABLE and demonstrably working** — the AMD64-binary Python 3.13 (PE header `0x8664`) executes successfully on this machine (`py -3.13 …` ran) |
| x86 (32-bit) emulation | NOT independently verified (not needed by the project; not probed) |

## 2. Python installations (PE-header verified, not filename-inferred)

| Install | Path | PE architecture | Reported `sys.version` | `platform.machine()` | Pointer | Runs? |
|---|---|---|---|---|---|---|
| `py -V:3.12-arm64` | `C:\Program Files\Python312-arm64\python.exe` | **ARM64** (`0xAA64`) | 3.12.10 | ARM64 | 64-bit | ✅ (this is the project base) |
| `py -V:3.13` (default) | `C:\Users\ASUS\AppData\Local\Programs\Python\Python313\python.exe` | **AMD64/x64** (`0x8664`) | 3.13.15 | ARM64 (OS-reported under emulation) | 64-bit | ✅ runs via Windows-on-ARM x64 emulation |
| `py` launcher | `C:\Windows\py.exe` | x86 (`0x14C`) | — | — | — | runs (launcher only) |
| WindowsApps `python.exe` | (Store alias) | — | — | — | — | not used |

`py -0p` output (verbatim): `-V:3.13 * … Python313\python.exe`; `-V:3.12-arm64 …
Program Files\Python312-arm64\python.exe`. Python installer signing:
both `python.exe` binaries = **Authenticode Valid**.

## 3. Existing SkipLine virtual environment

- Path: `C:\Arunakumara\skip-line-FINAL\skip-line\venv` — `pyvenv.cfg`
  confirms creation from `C:\Program Files\Python312-arm64\python.exe`
  (Python **3.12.10**, ARM64). `venv\Scripts\python.exe` = ARM64 PE.
- **The venv is native-ARM64 and arch-consistent.** No mixing found.
- Pinned relevant packages (pip): sqlalchemy 2.0.30, greenlet 3.5.2,
  aiomysql 0.2.0, PyMySQL 1.1.1, aiosqlite 0.22.1, cryptography 46.0.3,
  Pillow 10.3.0, pydantic 2.7.1 / pydantic-core 2.18.2, fastapi 0.111.0,
  uvicorn 0.30.1, httpx 0.27.0, alembic 1.13.1, pytest 8.2.2,
  playwright 1.64.1, python-jose 3.3.0, razorpay 1.4.2, redis 8.1.0.
- Native `.pyd` inventory (PE-header scan of the venv):
  `greenlet\_greenlet.cp312-win_arm64.pyd` = **ARM64**;
  `pydantic\_pydantic_core.cp312-win_arm64.pyd` = **ARM64** (arch-consistent;
  **all** native modules match the interpreter). Mysql drivers (aiomysql/
  PyMySQL) are pure-Python — no native layer, arch-agnostic.
- Note: the pinned `pydantic-core 2.18.2`/`Pillow 10.3.0` do not publish
  Windows `cp313` wheels — the **partially installed AMD64 3.13 interpreter
  cannot host this pin set**; this is a Python-version constraint, not an
  architecture problem (see §10).

## 4. Greenlet / Code Integrity findings (Phase 2)

| Question | Finding (evidence) |
|---|---|
| DLL architecture matches the process? | YES — `_greenlet.cp312-win_arm64.pyd` is ARM64, loaded into ARM64 python.exe. **NOT an architecture mismatch.** |
| Signed or unsigned? | **NotSigned** (Authenticode `Status=NotSigned`) |
| Failure = architecture mismatch? | **NO** — arch matches; fails because of signing level |
| Failure = application-control enforcement? | **YES** — Code Integrity event **3077**, `Policy ID:{0283ac0f-fff1-49ae-ada1-8a933130cad6}`, "did not meet the Enterprise signing level requirements" |
| Both issues simultaneously? | No — only the signing-policy issue exists for this DLL |
| greenlet 3.5.2 compatible with py3.12/ARM64? | YES — wheel installed cleanly; it is the policy that blocks loading it |

**Decisive cross-arch pilot (Nov 8):** built a disposable AMD64 venv from
the existing AMD64 3.13 (`%TEMP%\opencode\venv-x64-pilot`), installed
`greenlet==3.5.2` — the unsigned AMD64 `_greenlet.cp313-win_amd64.pyd`
**imports successfully** under WoA x64 emulation while the ARM64 build
stays blocked (fresh ImportError re-verified same minute). So policy
`0283ac0f` blocks the **unsigned ARM64** greenlet build specifically; the
same vendor's unsigned **AMD64** build currently loads. The policy is
per-architecture/per-binary — "greenlet is blocked" is only true for
ARM64-virtualenv execution on this machine. No bypass was attempted; the
workaround below uses officially-published binaries only.

## 5. Docker daemon and image architectures (Phase 3)

- CLI: docker 29.8.1, **windows/arm64** build, context `desktop-linux`;
  `docker compose version v5.5.1`; Docker Desktop installed per-user at
  `C:\Users\ASUS\AppData\Local\Programs\DockerDesktop` (a plain
  `C:\Program Files\Docker` check was misleadingly negative).
- Engine initially NOT RUNNING (both named pipes missing); launched Docker
  Desktop (user-level app start; no security settings touched) →
  `docker info`: **ServerVersion 29.8.1, Arch=aarch64, OSType=linux** — a
  native **ARM64 Linux engine**.
- Published architectures (Docker Hub manifests, registry API):

| Compose image | Published arch | Classification |
|---|---|---|
| `mysql:8.0.46` | linux/amd64, **linux/arm64/v8** | Multi-arch → ARM64-native |
| `redis:7-alpine` | 8 archs incl. **arm64/v8** | Multi-arch → ARM64-native |
| `nginx:1.26-alpine` | 8 archs incl. **arm64/v8** | Multi-arch → ARM64-native |
| `python:3.12-slim` (backend base) | 8 archs incl. **arm64/v8** | Multi-arch → ARM64-native |
| `clamav/clamav:1.4` | **linux/amd64 only** | Emulated-AMD64 required (or falls to NOT ATSUPPORTED-native) — engine-dependent QEMU/Rosetta; heaviest service, optional (upload scanning) |

- Backend `Dockerfile`: `python:3.12-slim` + apt build deps for
  `mysqlclient`-style builds; wheels built at image build time →
  ARM64-native build possible with the same pinned requirements.
- Host port conflicts verified pre-emptively: compose `db` wants
  `3306:3306` (host MySQL 8.0.46 occupies 3306) and `nginx` wants
  80/443 — bring-up subsets must avoid or re-map those (done via isolated
  probe network + no-published-ports probe containers; no compose
  modifications).

## 6. AMD64 emulation findings

- Python: AMD64 interpreter executes under WoA x64 emulation; pip installs
  and native wheel loading work (pilot above). Suitable as a fallback
  interpreter, but **no AMD64 Python 3.12 exists here**, and the
  project's pins (pydantic-core 2.18.2, Pillow 10.3.0) lack cp313 Windows
  wheels — so a **local AMD64 Python venv cannot host the pinned
  requirements**. Therefore the x64 interpreter is NOT the chosen local
  path.
- Linux containers: the engine is ARM64-native; AMD64-only images
  (clamav) would require explicit `platform: linux/amd64` + engine
  emulation — not enabled in the existing compose and **not forced**
  (per the audit's rules). Docker Desktop's built-in binfmt/rosetta
  support was not probed with a live amd64 container because the
  verification priority was the ARM64-native path; classify clamav as
  EMULATED-AM64 (optional), e.g. "not yet verified under live emulation".

## 7. Chosen architecture

- **Local (Windows host):** keep ARM64 Python 3.12 (`venv`) for greenlet-free
  work (85-test hermetic subset re-verified green this session). Async/
  greenlet-dependent work is blocked INSIDE the ARM64 venv by policy
  `0283ac0f` — not by architecture.
- **Local (Docker):** use the Docker Desktop **ARM64-native Linux engine**
  for architecture-sensitive and greenlet-dependent verification
  (`python:3.12-slim` arm64 image = exact pinned interpreter version; the
  Linux container is outside the Windows WDAC policy scope). This requires
  installing **no** new software and creating **no** mixed-arch venv.
- **CI:** hosted `ubuntu-latest` (Private ARM64/AMD64 hosted runner — the
  already-green hosted runs) remains the authoritative verification
  environment.
- **Rejected alternatives (with reasons):** AMD64 venv on 3.13 (cannot host
  pin set — no cp313 wheels); installing AMD64 Python 3.12 side-by-side
  (possible but redundant — the container path provides the identical
  pinned stack; user-machine changes avoided); forcing compose
  `platform: linux/amd64` for all services (would degrade 4 of 5 images
  for no benefit — only clamav needs that, optionally).

## 8. Tests executed (this audit) — statuses

| # | Test | Env | Result |
|---|---|---|---|
| 1 | Python import checks (both interpreters, PE arch, `pyvenv.cfg`) | both | PASS (evidence above) |
| 2 | `greenlet` import (ARM64 venv) | venv (ARM64) | **BLOCKED** — event 3077 policy `0283ac0f` (fresh failure) |
| 3 | `greenlet` import + basic execution (pilot venv) | AMD64 py3.13 venv | **PASS** (`X64-GREENLET-IMPORT-OK 3.5.2`) |
| 4 | Greenlet-free hermetic subset: resilience(6) + config-security(19) + CI-gate(60) | venv (ARM64) | **PASS — 85/85 in 0.54 s, exit 0** (`final_subset_junit.xml`) — NOTE: resilience file contains **6** tests, docs previously said 7 (correction propagated to the acceptance report) |
| 5 | SQLAlchemy async connection / full DB-backed suite / acceptance flows | venv | BLOCKED (same policy) |
| 6 | Docker engine bring-up | Docker Desktop (ARM64) | PASS after launch (maintenance `docker info` OK) |
| 7 | Compose manifest audit (5 images) | daemon-independent | PASS (§5 table) |
| 8 | Container-based suite + MySQL/Redis probes + app health | Docker ARM64 | **PASS — full suite junit 864/0/0 (4 real-Redis skips), exit 0, 63.6 s; real alembic chain ALL-OK exit 0; `/health` smoke PASS** (details in §9; acceptance modules excluded — `playwright` absent from pinned `requirements.txt`) |

## 9. Docker ARM64 execution evidence (executed Nov 8, 2026)

All three runs below used `skip-line-app:latest`, built **ARM64-natively** on the
aarch64 Linux engine from `python:3.12-slim` (arm64 variant) with the exact
pinned `requirements.txt`. Build-time only: compose interpolation required a
synthetic `MYSQL_ROOT_PASSWORD` value (compose `:?required` guard held by
default; no .env involved).

Environment-level confirmation inside the container:
`py 3.12.16 aarch64 | greenlet 3.5.2 | sqlalchemy 2.0.30 | pydantic_core 2.18.2`
(aarch64 manylinux wheels: greenlet/pydantic-core/Pillow/SQLAlchemy).

### 9.1 Full pytest suite in the ARM64 container — BLOCKED/collection for 6
acceptance modules, **PASS for the rest** (`container_pytest_noacc.log`)
- First run (entire tree incl. `tests/acceptance`): **exit 2 — 6 collection
  errors, ModuleNotFoundError: No module named 'playwright'** (all six
  `tests/acceptance/test_flow*.py`).
- Root cause (evidence, not WDAC): **`playwright` is not pinned in
  `requirements.txt`**, so neither the Docker image nor the hosted-CI pip
  step (`pip install -r requirements.txt`) ever installs it. Windows-side the
  same modules are blocked by WDAC before this difference could matter; the
  container run isolates the package-absence fact.
- Rerun with `--ignore=tests/acceptance` (everything else): **PASS — junit
  `tests=864 failures=0 errors=0 skipped=4`, exit 0, in 63.6 s**
  (`co/container_ct_junit.xml`, `co/stdout.log`). The 4 skips are the real
  Redis integration checks (`skipped` only, never counted as passing).
  Count parity: 864 = hosted-CI 857 + 6 new provider-resilience tests
  (added uncommitted this closure) + 1 new `test_prod_rejects_debug_true`.
  This is the first fully-green execution of the (uncommitted) suite on a
  greenlet-capable native-ARM64 environment.
- During the first run a **stale duplicate** `tests/unit/test_config_security.py`
  (tracked copy predating the APP_DEBUG hard-gate) failed 4 admin-password
  cases because its `build_settings()` lacked the `app_debug=False` default
  the modernized root copy uses; minimally aligned (same comment) — the
  class now passes in-container (`TestAdminPasswordConfiguration` rerun exit 0).

### 9.2 Real Alembic chain 0001→0018 in the ARM64 container — **PASS**
- Method honoring the script's loopback gate: checker container sharing the
  disposable probe MySQL's network namespace
  (`--network container:skipline_probe_mysql`, `DB_HOST=127.0.0.1:3306`
  `DB_USER=root`/`DB_PASSWORD=archprobe-synth-1`/`DB_NAME=skipline_ci_migrate`).
- Fresh probe MySQL 8.0.46 (aarch64) started WITHOUT `MYSQL_DATABASE`, so the
  script's pre-existing-database refusal path passes and the script itself
  creates and drops `skipline_ci_migrate`.
- Result: `[2] full chain 0001→0017 OK in 14.9s`; `[3] legacy rows seeded…`
  `alembic_version=0018; FK constraints=14/14; named indexes=9/9`;
  `[4] upgrade head OK`; `[5] downgrade 0018→0017 OK (no MySQL error 1553)`;
  `[6] re-upgrade (idempotence) OK`; `[7] disposable database dropped`;
  `ALL-OK` / exit **0** (`container_chain.log`).
- Note: this corroborates — and does NOT replace — the earlier qualified
  run-9 backup/restore record (kept as the authority for that rehearsal).

### 9.3 App image bring-up + /health in the ARM64 container — **PASS**
- Bootstrap chain executed for real via `scripts/start.sh`: MySQL wait →
  `alembic upgrade head` against disposable DB `skipline_probe_health` →
  uvicorn 0.0.0.0:8000, workers=4.
- `curl -sf http://localhost:8000/health` inside the app container:
  `{"status":"ok","app":"Skip Line"}` (exit 0).
- Probe DB `alembic_version` after boot: **0018**.
- Env used at runtime: `DB_*` point to the disposable probe; synthetic dev
  JWT/SECRET/ADMIN values; `REDIS_URL=redis://skipline_probe_redis:6379/0`
  (redis:7-alpine arm64 on the same probe network).

### 9.4 New security finding: image previously baked the real `.env` — FIXED
- Artifacts before this fix: there was no `.dockerignore`, and the first
  built image contained `/app/.env` (3 756 B — byte-identical size to the
  repo's real `.env`; contents were never read or printed during this audit).
- Fix applied this session: `.dockerignore` created (`.env` only — minimal
  change), image rebuilt (cache reuse of all dependency layers; exit 0),
  and the post-fix image verified: `/app/.env` **absent**,
  `/app/.env.example` still present for operator reference.
- Status for the secret-scan ledger: host-scan results unchanged; this adds
  the Docker-image layer to the same closure (scan + .dockerignore now
  verified by image content check).


## 10. Recommended environment standardization (reproducible commands)

```text
# Windows-native (greenlet-free checks; unchanged existing venv):
venv\Scripts\python.exe -m pytest tests\test_config_security.py ^
  tests\test_ci_migration_gate.py tests\unit\test_ai_provider_resilience.py ^
  -q --noconftest

# Architecture-sensitive / greenlet-dependent work — ARM64-native containers:
# (Docker Desktop must be running)
docker compose build app            # ARM64-native linux build of the pinned stack
docker run --rm <app-image> python -m pytest -q        # full suite (SQLite fixtures)
docker run -d --name skipline_probe_mysql --network <probe-net> \
  -e MYSQL_ROOT_PASSWORD=<synthetic> mysql:8.0.46      # arm64-native MySQL probe
docker run --rm --network <probe-net> -e DB_HOST=skipline_probe_mysql \
  -e DB_NAME=skipline_ci_migrate -e DB_USER=root -e DB_PASSWORD=<synthetic> \
  <app-image> python scripts/ci_migration_chain_check.py  # real alembic chain

# Hosted CI (authoritative, unchanged):
#   .github/workflows/production-readiness-tests.yml on ubuntu-latest
```

## 11. Remaining limitations

- Windows ARM64 pin-level: the WDAC policy `0283ac0f` continues to block
  the unsigned ARM64 `_greenlet` inside `venv\` on the host — an enterprise
  IT remediation (signing or allowlist per
  `docs/IT_BLOCKER_CODEINTEGRITY_0283ac0f.md`) is unchanged in principle,
  only the per-arch nuance above is new evidence.
- clamav/clamav:1.4 is amd64-only — runs under engine emulation only if
  binfmt is enabled; until probed, classify uploads-scanning bring-up
  EMULATED/NOT-VERIFIED (optional service, gated by CLAMAV_ENABLED=false).
- No published requirement change: production pins stay on Python **3.12**;
  the AMD64 3.13 interpreter is incompatible with the pin set (cp313 wheel
  absence) and is documented as fallback-only.
- Playwright browser journeys are still environment-gated (browsers exist
  for the host venv; the container path would need a Playwright-with-
  browsers image variant — queued as a blocker-matrix item, unclaimed).
