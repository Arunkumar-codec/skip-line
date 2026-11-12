# MySQL Root Password — Origin Investigation & Safe Remediation Plan

Date: Nov 8, 2026 · **Read-only investigation (nothing executed against the
server except whitelisted read-only probes; no password/DB/user changes; no
git operations).** Companion to `docs/CREDENTIAL_ORIGIN_AUDIT.md`.
Evidence sources used: `opencode.db` session-message store (read-only SQLite,
masked), historic session shell outputs (masked), PSReadLine history (masked),
Git object store/refs/tags, live MySQL metadata (root row metadata only),
filesystem timestamps.

## 1. How the current MySQL root password originated — CONFIRMED

**Verdict: the password is OpenCode-generated AND OpenCode-applied
server-side, with the owner's explicit authorization, during a security-rotation
session on Oct 31, 2026. It is NOT uncontrollably mysterious and NOT an
unresolved origin — but it was born exposed-by-lineage (see §1.4) and one new
leak has occurred since (§1.5).**

### 1.1 The setup that made rotation necessary
- The project's initial commit (pre-rewrite SHA `82d6d45`, tagged
  `v1.0-baseline` at the time) carried a **hardcoded real DB password in
  `app/config.py`**. That credential was committed to Git and **pushed to
  `origin/main`** (pre-cleanup origin HEAD was `d768faa`). Being family-adjacent
  (`Arun…`-style) it predates this audit and the owner did not "supply" it in
  this project scope — origin of that value itself is UNRESOLVED.
- The owner then explicitly tasked OpenCode (`# SKIPLINE — FINAL SECURITY
  CLEANUP AND PUSH`, stored in the session store, Oct 31 22:53) to "rotate the
  exposed MySQL credential safely", stating the exposed string was also the
  *then-live* credential of `root@localhost`.

### 1.2 The exact command/process that set the active password
- OpenCode-authored probe (`%TEMP%\opencode\db_probe.py`, now deleted)
  connected as `root@localhost` on MySQL **8.0.46**, verifying authorization.
- OpenCode-authored `%TEMP%\opencode\rotate_db.py` (now deleted) then:
  1. generated a fresh password via the Python `secrets` module
     ("Generate secure password via secrets (not printed)" — verbatim session
     plan; never printed to any log),
  2. ran the server-side change for `root@localhost`
     (`ALTER USER … IDENTIFIED BY <new>` as the session text states; the tool
     *output* — printed at 22:54:49 — discloses only booleans:
     "password rotated for 1 account(s)", "env updated DB_PASSWORD: True",
     "post-rotation connectivity: OK", "old-credential check: rejected (good)",
     "rotation exit code: 0"),
  3. rewrote `.env` `DB_PASSWORD` (and replaced the leaked `Admin@1234!`
     `ADMIN_PASSWORD` default), old credential then rejected.
- **Timestamp proof (byte-exact):** `mysql.user` shows
  `root@localhost … password_last_changed = 2026-10-31 22:54:53` and the repo
  `.env` **mtime = 10/31/2026 22:54:53** — the same second.
- Post-rotation verification in the same session: app config path + Alembic
  connected (MySQLImpl) using the rotated credential; the live `skipline` DB
  was observed untouched (12 of its tables pre-existed; no `alembic_version`).

### 1.3 Provenance of the generation
- Generated **locally** by OpenCode at runtime (secrets module) — not
  inherited from any existing configuration, not part of MySQL initialization
  (the host MySQL 8.0.46 datadir predates the change; the install itself is
  older than Oct 31).
- Micro-detail UNRESOLVED: the exact generator call (file deleted); irrelevant
  to validity — the value's strength (32 chars, mixed case+digits) is verified
  and it is not the leaked family value.

### 1.4 Residual leak state of the *old* (pre-rotation) credential
- Still present **only** as an orphaned Git object: pre-rewrite SHA `82d6d45`
  exists in the local `.git` object store (`app/config.py` blob), reachable
  **by no ref/tag/reflog path** (`git log --all -S` finds nothing;
  `v1.0-baseline` points at the post-rewrite clean `e65360d`; `HEAD`/origin
  clean).
- Before the Oct 31 history cleanup, the leak-chain commits were pushed to
  `origin` (`origin/main` was `d768faa` then). The rewrite pre-dated all later
  pushes (origin/main = `f44c320` since Nov 6). Whether an external copy of
  the pre-rewrite objects exists (GitHub-side object store, forks, remote
  tags/PR caches) is UNRESOLVED from this machine — moot for the *current*
  password (which is not that value), but decisive for the *old* one (already
  revoked Oct 31) and for local hygiene (see remediation R7).

### 1.5 NEW leak (this session) — disclosure required
- During this audit's evidence scan, one historical session log
  (`sh_1b4e0fa3e001pRH491F2PKXq22.out`, a Nov 7 acceptance-session capture)
  was printed un-masked in the transcript and **disclosed the current root
  password** (embedded in a `mysql+pymysql://root:<value>@localhost:3306/…`
  URL). The same value remains on disk inside that `.out` file in
  `%USERPROFILE%\.local\share\opencode\shell\…`. This makes **an immediate
  operator rotation of the root password mandatory** (Remediation R1) and is
  an honest incident of the audit itself: the printed value is now in the
  conversation transcript.
- No other secret values were printed by this audit; every later scan
  masked URL-credential forms as well.

### 1.6 Timeline (evidence-sourced)
| Time (Oct 31 / Nov 2026) | Event | Evidence |
|---|---|---|
| pre-Oct 31 | host MySQL 8.0.46 installed; original dev credential in `app/config.py`, committed + pushed | session text; commit chain `82d6d45…d768faa` |
| Oct 31 17:24 | `.env` first created (mtime basis; later rewritten at rotation) | image `COPY` listing |
| Oct 31 22:48 | commit `f98e1a4` "fix: remove hardcoded credentials and enforce required secrets" | git history |
| Oct 31 22:53 | owner task: final security cleanup; rotation authorized; probe run | session store 22:53:14/22:54:23 |
| **Oct 31 22:54:53** | **`rotate_db.py` applied the new password to `root@localhost`; `.env` rewritten same second** | `mysql.user` metadata == `.env` mtime; session 22:54:49/22:54:53 |
| Oct 31 23:0x | history cleanup (rewrite); tag `v1.0-baseline` → clean `e65360d` | refs; pickaxe zero-hit on reachable refs |
| Nov 5–8 | rotated credential used successfully by run-9 rehearsal + probes | `%TEMP%\opencode\prodcheck\*` logs |

## 2. Safe, non-executing remediation plan (operator-run)

> Nothing below is executed by this audit. All commands are for the operator,
> with placeholders (`<…>`) to be filled from a password manager/`python -c
> "import secrets; print(secrets.token_urlsafe(32))"`. Never paste values into
> transcripts; use a masked-env script like the audit's (read values from
> files, print booleans).

**Constraints respected:** no password changes, no `.env` edits, no
user creation, no DB modification, no migrations, no touching
`skipline_phase9_prodcheck`/`skipline_synccheck`, no deletion of Docker
images/volumes/caches/containers, no commit/push, no values displayed.

### R0 — Backup the current DB first (safety net)
1. `mysqldump --single-transaction --routines --events --triggers
   --set-gtid-purged=OFF -h localhost -u root -p skipline >
   backups/skipline_pre_usermigration_<ts>.sql` (owner-run; gzip if desired).
2. Record current grants: `SHOW GRANTS FOR root@localhost; SHOW GRANTS FOR
   current_user();` into a protected local note (no values).

### R1 — Rotate the MySQL root password NOW (incident §1.5)
`ALTER USER 'root'@'localhost' IDENTIFIED BY '<new-strong-random-32+>';
` then update the *private* store; **root password must never live in `.env`
after this migration** (the app account below replaces it). Verify with the
audit's boolean-style connect script (old value rejected, new accepted).

### R2 — Create the dedicated least-privilege SkipLine app user (host/dev)
```sql
CREATE USER 'skipline_app'@'localhost'
  IDENTIFIED BY '<operator-generated-strong-random-24+>';
GRANT SELECT, INSERT, UPDATE, DELETE,
      CREATE, DROP, INDEX, ALTER, REFERENCES,
      SHOW VIEW, TRIGGER, LOCK TABLES
  ON skipline.* TO 'skipline_app'@'localhost';
FLUSH PRIVILEGES;
```
Privilege rationale (matches the app's real needs; `init.sql`'s original list
plus two additions):
- DML + DDL (`CREATE/INDEX/ALTER/DROP`) — Alembic chain + seed.sql;
- **`REFERENCES` — REQUIRED for FK creation in MySQL 8.0 (missing from
  `init.sql`'s list — flagged fix);**
- `SHOW VIEW`+`TRIGGER`+`LOCK TABLES` — needed by the backup path
  (`mysqldump --single-transaction --triggers`).
- NOT granted: global/GROUP privileges, `FILE`, `PROCESS`, user-admin, other
  databases. **No password reuse** with the root value.
- If the app ever runs from Docker containers hitting host MySQL, also create
  `skipline_app`@`<docker-bridge-subnet>%` or `@'%'` — prefer the smallest
  host pattern (`localhost` for host-run dev; `%` only inside the compose
  stack where MySQL's own entrypoint already creates `skipline_user`@`%`).

### R3 — Separate `.env` identities (dev) from production
- Dev (post-migration): `DB_USER=skipline_app`, `DB_PASSWORD=<app-user pw>`,
  root value removed from `.env` entirely (kept in a local password manager).
- Production: fresh per-environment values — dedicated production DB user,
  a **different** root password, different `SECRET_KEY`/`JWT_SECRET_KEY` —
  never copied from the dev machine (the ".env procedure" is the documented
  control; validators enforce strength markers only).
- `MYSQL_ROOT_PASSWORD` (R4) must be its **own** value, never the host root's.

### R4 — `MYSQL_ROOT_PASSWORD` for Docker Compose (correct configuration)
- Generate a unique value for the compose stack's *container* root.
- Critical caveat: MySQL's entrypoint applies `MYSQL_ROOT_PASSWORD` only on
  **first initialization** of the `mysql_data` volume. If a volume/dataset
  already exists, changing `.env` has no effect; options: (a) start from a
  fresh volume (after R0-class backup; the stack DB is disposable dev data),
  or (b) `docker compose exec db mysql -u root -p<old> -e "ALTER USER
  root@'%' IDENTIFIED BY '<new>';"` — operator-run only.
- Compose should keep using `MYSQL_DATABASE`/`MYSQL_USER`/`MYSQL_PASSWORD`
  (their env names) to create the app identity; `.env`'s `DB_USER/DB_PASSWORD`
  feed those. With `DB_USER=root`
  (current), compose **fails by design** — after R2 this resolves naturally.

### R5 — Parameterize / remove the hardcoded `skipline_pass`
Recommended (a):
1. Delete from `database/init.sql` the `CREATE USER … IDENTIFIED BY
   'skipline_pass'` line, the `GRANT … TO 'skipline_user'@'%'` line and
   `FLUSH PRIVILEGES;` — the MySQL entrypoint's `MYSQL_USER/MYSQL_PASSWORD`
   env flow already creates the user with the real password before
   init-scripts run (with `IF NOT EXISTS` semantics that also mask typos).
2. Keep only the `CREATE DATABASE` block (or drop it too — `MYSQL_DATABASE`
   covers it), keeping the file as a no-op/deleted block.
Alternative (b) if a script form is needed: replace `init.sql` with a
`docker-entrypoint-initdb.d/01-app-user.sh` that reads `$MYSQL_USER`,
`$MYSQL_PASSWORD` (already exported by the image) and issues the `GRANT` —
no literals.
Both routes remove the last hardcoded credential from the repo.

### R6 — Data preservation guarantees
- The migration touches **accounts** only; no table/data changes anywhere.
- `skipline`, `skipline_phase9_prodcheck`, `skipline_synccheck` are never
  targets; grants are scoped `ON skipline.*` (explicitly NOT others).
- `initdb.d` scripts run only on a **fresh** datadir — an existing host server
  never re-runs `init.sql` (no risk of the placeholder re-materializing).

### R7 — Local hygiene (optional, owner-run; destructive to orphan objects)
- After confirming the tag (`v1.0-baseline`) keeps pointing at `e65360d`,
  purge orphaned pre-rewrite objects so the leaked blob stops sitting in the
  local store: `git gc --prune=now` (verify first via
  `git cat-file -t 82d6d45` → then absence).
- Delete/owner-review the session-log files that still carry credential text
  (`.out` files + `%TEMP%\opencode\rotate_db.py` already gone; the Nov 7
  `.out` with the current root URL remains — remove after R1).
- Remote (owner-side, optional): contact the hoster to GC the pre-rewrite
  objects if paranoid; forks/PR caches cannot be forcibly cleaned — the
  guarantee comes from R1 rotation, not deletion.

### R8 — Validation sequence after migration (no live changes by this audit)
1. **Connectivity, least privilege:** boolean connect probes as
   `skipline_app` (SELECT 1, `SHOW GRANTS CURRENT_USER()`); assert NO
   `mysql.*`/global access.
2. **Protected-DB isolation:** attempt `USE skipline_phase9_prodcheck` with
   the new account → must fail; `SHOW DATABASES` sees only the permitted scope.
3. **App boot with override env:** run the existing container image with
   `DB_USER='skipline_app'` + its password → `/health` smoke PASS
   (`{"status":"ok"}`); alembic `current` prints a revision.
4. **Disposable-DB migration:** run `scripts/ci_migration_chain_check.py`
   pointing at the app account (add a `--db-user` override if the script is
   root-bound; or create the probe DB with grants for the app user first).
5. **Suites:** greenlet-free subset (85/85) with the new creds; container full
   suite (already 864/0/0) re-smoke only if the connection layer changed.
6. **Backup path auth check:** confirm `backup.sh`/rehearsal use the app
   account (SELECT/LOCK TABLES/TRIGGER/SHOW VIEW suffice).

### R9 — Rollback instructions
| Failure | Rollback |
|---|---|
| App can't connect after `.env` switch | Revert `.env` `DB_USER/DB_PASSWORD` to the pre-migration dev root values (rotation R1's value — kept in the manager). App user can remain harmlessly or be removed via `DROP USER 'skipline_app'@'localhost';` |
| Under-scoped grants break Alembic/backups | Re-issue the fuller GRANT list (add REFERENCES / SHOW VIEW / TRIGGER / LOCK TABLES / EVENT/ROUTINE as diagnosed) — additive GRANTs only |
| Wrong account name in `MYSQL_USER` for compose | Recreate the stack from a fresh volume (dev-only disposable data; after a backup) — or keep the entrypoint-created name and align `.env` to it |
| Root rotation breaks local admin tooling | Rotate to the previously recorded value (kept in the manager), or create a temporary admin account, fix tooling, then re-rotate |
| Any mismatch discovered | Whichever way, protected DBs were never written; audit artifacts (boolean logs) show the exact last verified state |

### Risks
- Over-narrow grants (missing `REFERENCES` is the classic failure → FK
  creation fails mid-migration; explicit fix listed).
- Compose volume already initialized → env changes silently ignored (steps in R4).
- Host-app vs container origin mismatch ('localhost' vs '%') — scoped by R2 note.
- The leaked token remains technically present in external copies (hoster GC
  uncertainty) — mitigated by rotation of everything that value guarded.
- Post-R1 root password rotation invalidates stored stack tooling that may
  still hold the old value (local shortcuts/histories) — check PSReadLine
  line hits and session logs before removing.

## 3. Verification commands (post-migration, no secrets on CLI)
```powershell
# all to run with values sourced from a local masked script (like this
# audit's): reads .env/user file in-process, prints only booleans/shapes
python  audit-connect.py --expect-user skipline_app          # new identity OK
python  audit-connect.py --deny-global                       # no global grants
python  deny-protected.py --db skipline_phase9_prodcheck     # must be refused
alembic current                                              # prints revision
`$env:DB_USER='skipline_app'; venv\Scripts\python.exe -m pytest `
  tests\test_config_security.py tests\test_ci_migration_gate.py -q --noconftest
docker run --rm -e DB_HOST=host.docker.internal -e DB_USER=skipline_app `
  skip-line-app python -c "import os,pymysql; pymysql.connect(host=os.environ['DB_HOST'],user=os.environ['DB_USER'],password=os.environ['DB_PASSWORD']).close(); print('OK')"
```

## 4. Summary
- Root password origin: **CONFIRMED** — OpenCode-generated + applied via
  `rotate_db.py` on 2026-10-31 22:54:53 during an owner-authorized rotation of
  a Git-history-leaked credential; timestamp-synced with `.env`.
- Current root password exposer: the original stack credential (revoked)…
  and now one accidental transcript disclosure by this audit (§1.5) → R1
  rotation is the mandatory first remediation step.
- Everything else (dedicated app user/grants, compose root value, init.sql
  parameterization, dev/prod separation) is a clean, reversible,
  data-preserving migration fully specified above, awaiting operator execution.
