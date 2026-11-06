# IT Remediation Request — Code Integrity policy blocks application testing

**Date:** Nov 6, 2026 · **System:** SkipLine production-readiness verification workstation
**Requester:** SkipLine verification session (post-Phase-9)

## 1. What is blocked

Windows Code Integrity **policy ID `0283ac0f`** ("did not meet the Enterprise
signing level requirements") blocks the venv's Python native extension:

- Blocked file: `C:\Arunakumara\skip-line-FINAL\skip-line\venv\Lib\site-packages\greenlet\_greenlet.cp312-win_arm64.pyd`
- Loader process: `C:\Program Files\Python312-arm64\python.exe`
- Evidence: `Get-WinEvent -LogName "Microsoft-Windows-CodeIntegrity/Operational"`
  event **3077** at 2026-11-06 10:21–10:22 (multiple hits, same policy).
- The file is unchanged since its 2026-06-20 pip install (greenlet wheel for
  Windows ARM64; these wheels are commonly unsigned). Reinstalling does not
  change the file hash.

Observed failure:

```
ImportError: DLL load failed while importing _greenlet:
An Application Control policy has blocked this file.
```

## 2. Why testing stops (dependency chain)

`greenlet` is a hard runtime dependency of SQLAlchemy's asyncio stack, which
this project uses for **all** database execution:

- app runtime: `SQLAlchemy 2.0.30` async engines (`aiomysql`)
- Alembic CLI: `migrations/env.py` uses `async_engine_from_config` +
  `connection.run_sync` → greenlet required even for `alembic upgrade`
- tests: in-memory aiosqlite fixtures; `tests/conftest.py` prepares a
  **session-scoped autouse** async fixture → every pytest run (even
  pure-sync tests) fails at fixture setup

**Blocked verifications until remediation:** the DB-backed part of the
pytest suite (720 of the 797 collected tests — every DB/integration/API/
workflow case depends on the session-scoped autouse async fixture, plus the
committed `tests/unit/test_config_security.py` DB cases) and the six
remaining UI-acceptance flow replays (HTTP replay drives the app's async
session handling). Per task rules, the verifier will not sign, replace,
patch, or otherwise bypass the policy; these checks are marked **BLOCKED**,
not PASSED.

**Session-4 update (Nov 6, ~11:00–12:00):** two checks were legitimately
restored WITHOUT touching the policy:

- `pytest --noconftest` (standard flag that skips only `tests/conftest.py`)
  executed all DB-free tests green: `tests/test_config_security.py`
  **18/18 PASS** and five DB-free unit files — **59 PASS**;
- the **MySQL migration chain re-execution completed greenlet-free** via a
  temporary sync `env.py` (only the repo's env.py is async): full
  0001→0018 up/down/re-up on disposable `skipline_phase10_chain`, 14 FK
  actions verified, 9/9 declared indexes, legacy rows preserved, **no
  error 1553** (see `docs/PRODUCTION_READINESS_REPORT.md` §1.1).

Session-3 note: event 3077 was still firing at ~11:02 the same day.

## 3. Authorized remediation options (choose one)

1. **Policy file rule (workstation):** add a trusted file/hash rule for
   `_greenlet.cp312-win_arm64.pyd` (or a greenlet release wheel from
   PyPI that carries a signature for the Enterprise signing level).
2. **Alternative machine:** run verification on a workstation not covered by
   this policy.
3. **CI runner:** GitHub Actions — a workflow is already prepared in the
   repo (`.github/workflows/production-readiness-tests.yml`, statically
   validated; activation is a repository Settings change by an authorized
   user, no code changes needed).
4. **Signed wheel swap (if policy owner provides one):** e.g. a
   signed greenlet build ordered by IT — installed into the existing venv by
   pip; no system policy changes required.

## 4. Exact commands to resume verification (approved machine/CI)

```bat
cd C:\Arunakumara\skip-line-FINAL\skip-line

:: 0. policy gate — must print a version, not a policy error
venv\Scripts\python.exe -c "import greenlet; print('greenlet', greenlet.__version__)"

:: 1. migration chain: COMPLETED in session 4 without greenlet (§1.1 of the
::    readiness report, sync env.py on disposable DB). Re-run only if the
::    migration code changes:
venv\Scripts\python.exe C:\Users\ASUS\AppData\Local\Temp\opencode\prodcheck\sync_chain_check.py

:: 2. production config security regressions (20-case matrix + 18-item
::    pytest file — file already executed 18/18 same-day via --noconftest)
venv\Scripts\python.exe C:\Users\ASUS\AppData\Local\Temp\opencode\prodcheck\config_matrix.py
venv\Scripts\python.exe -m pytest tests\test_config_security.py -q

:: 3. AI real-HTTP transport matrix (localhost mock server, dummy key)
venv\Scripts\python.exe C:\Users\ASUS\AppData\Local\Temp\opencode\prodcheck\ai_transport_check.py

:: 4. full automated suite (record passed/failed/skipped counts!)
venv\Scripts\python.exe -m pytest -q

:: 5. confirm the policy no longer fires (optional)
Get-WinEvent -LogName "Microsoft-Windows-CodeIntegrity/Operational" -MaxEvents 20 ^
  | Where-Object { $_.Id -eq 3077 }
```

Notes for the operator:
- MySQL must be reachable with the disposable-database privileges the
  script uses (`CREATE/DROP DATABASE skipline_phase9_*`). The **real
  `skipline` database must never be touched** by these scripts.
- Password values come from the local `.env` and must not be echoed into
  logs or the transcript.
- If a live LLM provider check is planned, use staging credentials and an
  explicitly authorized metered account; no real bookings or payments.
