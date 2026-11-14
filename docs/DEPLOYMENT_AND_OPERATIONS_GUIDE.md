# SkipLine — Deployment, Operations & Demonstration Guide

> **Delivered:** November 14, 2026 (MASTER §23 Phase 13)
> **Audience:** operators deploying the system, organization staff running it day-to-day, and anyone demonstrating it (academic/production handover)
> **Safety rule honored throughout:** this guide contains **no real passwords, secret keys or `.env` values**. Every credential below is a placeholder; real values live only in the git-ignored `.env`.

Related documents: `docs/deployment.md` (Docker/systemd runbooks), `docs/database.md` (schema), `docs/IMPLEMENTATION_ROADMAP.md` (build record), `docs/OPEN_DECISIONS.md` (design decisions), `docs/SEC009_LEGAL_HANDOVER.md` (pending legal review).

---

## 1. System components

| Component | Technology | Role |
|---|---|---|
| Application | FastAPI (Python), Jinja2 UI, WebSocket | All API + portal surfaces |
| Database | MySQL 8.0 (InnoDB) | Authoritative data store; schema built **only** by Alembic migrations `0001→0019` |
| Redis *(optional)* | redis:7 | Cross-worker WebSocket bus + distributed rate limiting; empty `REDIS_URL` = single-worker in-process mode |
| ClamAV *(optional)* | clamav container | Malware scanning of uploads; disabled ⇒ scans recorded as `disabled`, never `clean` |
| nginx | nginx:1.26 | TLS termination / reverse proxy (`nginx.conf` dev, `nginx.production.conf` HTTPS) |
| Backup | one-shot mysql:8.3 container (`scripts/backup.sh`) | Logical `mysqldump` into git-ignored `./backups/` |

## 2. Environment configuration (secret-free)

1. Copy `.env.example` → `.env` (`.env` is gitignored; `git log --all -- .env` must stay empty).
2. Fill in real values. **Never** paste any real credential into a doc, ticket or this guide.
3. Production (`APP_ENV=production`) hard-fails at startup unless **all four** are set and strong:
   `DB_PASSWORD`, `SECRET_KEY` (≥32 chars, stable across restarts — it signs session cookies and encrypts identity documents), `JWT_SECRET_KEY` (≥32 chars), `ADMIN_PASSWORD` (≥12 chars, seeds the first Super Admin once). `APP_DEBUG` must be `false`.
   Generate secrets with: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
4. Optional toggles (all default OFF / graceful):
   - `REDIS_URL` — set for multi-worker WebSocket + distributed rate limits.
   - `CLAMAV_ENABLED` + `CLAMAV_HOST/PORT` — real upload scanning (verify container health first).
   - `CSRF_ENFORCE` — automatic in production; force in development only if needed.
   - `RATE_LIMIT_AUTH_REQUESTS` / `RATE_LIMIT_ACCOUNT_REQUESTS` — defaults 20/60 s and 300/60 s.
   - `NGINX_CONF` — `nginx.conf` (HTTP) or `nginx.production.conf` (HTTPS; certs in git-ignored `./nginx/ssl`).
   - `AI_PROVIDER` / `AI_BASE_URL` / `AI_MODEL` / `AI_API_KEY` — leave `AI_PROVIDER` empty to disable the AI assistant entirely (every `/ai` endpoint then answers "unavailable" and manual booking remains). No paid external calls are made by the test suite; demos use `scripts/mock_ai_provider.py`.
   - Notification providers (SMTP / SMS webhook) are optional-by-design: unconfigured providers record outbox rows as `SUPPRESSED` instead of failing user flows.

## 3. Least-privilege MySQL provisioning

- The application connects as a **dedicated user** (`.env.example`: `DB_USER=skipline_app`) — **never root**.
- Required grants for `skipline_app`: `SELECT/INSERT/UPDATE/DELETE` on `skipline.*`, plus `CREATE/DROP/ALTER` restricted to the **disposable prefixes** the test harness uses (`skipline_test_%`, `skipline_acceptance_%`, `skipline_wps_%`). No global privileges.
- `scripts/wps2_privilege_transition.py` performs the full transition in one process: backup of the protected databases (`skipline`, `skipline_phase9_prodcheck`, `skipline_synccheck` — read-only for the tool), provisioning of `skipline_app`, empirical grant probing, root rotation, and an atomic `.env` rewrite. Password values are never printed or persisted outside the atomic `.env` update.
- Verify after provisioning: `python scripts/wps2_privcheck_verify.py` (privilege check) and confirm the app boots and a login works.

## 4. Database migrations (Alembic `0001 → 0019`)

```bash
make migrate            # alembic upgrade head
alembic history         # inspect the chain
alembic check           # must report: No new upgrade operations detected
```

- The full chain `0001→0019` has been executed end-to-end on real MySQL 8.0.46/InnoDB (upgrade → downgrade → re-upgrade, data preservation verified, MySQL 1553-safe via index renames) — see the Phase 6/10 records in `docs/IMPLEMENTATION_ROADMAP.md`.
- The app **boots on a purely Alembic-built database**; `create_all` is verified as a no-op there (byte-identical pre/post-boot schema snapshots).
- Rollback: `alembic downgrade -1` (or to a specific revision) — run a fresh backup first (§6).

## 5. Deployment options

- **Docker Compose (recommended):** follow `docs/deployment.md` §"Option 1" — configure `.env`, stage TLS certs under `./nginx/ssl`, set `NGINX_CONF=nginx.production.conf`, `docker compose up -d`, then check health (`/health`, container status, logs).
- **Manual (Ubuntu 22.04 + systemd + host nginx):** follow `docs/deployment.md` §"Option 2".
- **Local development:** `make install && make dev` (uvicorn auto-reload on :8000).
- First boot: migrations run (§4); the admin bootstrap seeds **one** Super Admin from `ADMIN_PASSWORD` only if none exist (public registration can never create one; the platform allows exactly four authorized Super Admin accounts).

### Post-deployment smoke checklist

1. `GET /health` returns healthy; `alembic check` clean.
2. Log into each portal: `/admin/login`, `/org/login`, `/user/login`, `/employee/login`.
3. Book one token end-to-end (customer → payment-free flow or cash) and watch it appear on `/public/display-view/{org_id}`.
4. `make security-scan` exits PASS (findings confined to test fixtures, if any).

## 6. Backup & restore

```bash
make backup                                   # ./backups/skipline_<UTC>.sql.gz (newest 7 kept)
make restore FILE=backups/skipline_XXX.sql.gz # MODE=check  → non-destructive scratch-DB validation
make restore FILE=backups/skipline_XXX.sql.gz MODE=force   # real restore (DESTRUCTIVE; 5 s Ctrl+C window)
```

- `scripts/backup.sh` runs `mysqldump` inside a mysql:8.3 one-shot container (compose supplies credentials — none are stored in the scripts).
- `scripts/restore.sh` **never invents success**: in check mode it restores into `skipline_restore_check`, verifies the schema loads, and reports exactly what it validated.
- Rehearsals are scripted (`scripts/backup_restore_rehearsal.py`, WPS1) — schedule a periodic restore-drill; a backup that has never been restored is not a backup.

## 7. Security operations

- `make security-scan` before every release/commit: 10-rule secret-pattern tripwire over all tracked files (findings in test fixtures are printed for triage but non-blocking; **any** finding in shipped source fails the run). `make pip-audit` for dependency CVEs.
- Secrets hygiene: real credentials only in git-ignored `.env`; logs scrub credentials (Redis URL redaction, credential-sanitizing audit logging); CI carries only synthetic values.
- Production security controls shipped in code: CSRF middleware (auto in production), security headers, layered rate limiting, principal-derived authorization (client tenant ids never trusted), two-step approval for high-impact enforcement actions, session revocation (logout / logout-all), purpose-signed QR references, guest PII suppression.
- **Standing owner obligations (not code):** SEC-009 legal review (identity/Aadhaar handling, payments, refunds, tax, retention, licensing) and hosted-CI verification remain with the owner before production go-live.

## 8. Role-by-role demonstration walkthroughs

All walkthroughs below work on a fresh deployment with seeded demo data (`make seed`).

### 8.1 Super Admin (`/admin/login`)
1. Dashboard → platform-wide organizations / users / tokens.
2. **Organizations:** approve or reject a pending registration (approval gates all org operations).
3. **Finance:** revenue + queue reports; KYC review queue (`/admin/kyc`).
4. **Trust desk:** support cases (`/admin/cases`), knowledge base (`/admin/knowledge`), feedback moderation (`/admin/feedback`).
5. Change password (`/admin/change-password`) — demonstrates enforced credential hygiene.

### 8.2 Organization Admin (`/org/login`)
1. **Services** (`/org/services`): create a service with pricing and estimated minutes; attach a multi-stage workflow (`/org/workflows` — stages, e.g. Check-in → Processing → Handout).
2. **Counters** (`/org/counters`): create counters, bind services, set token prefixes.
3. **Schedule** (`/org/schedule`): weekly hours, exceptions, capacity rules — the public slot ladder (`/public/organizations/{id}/slots`) reflects changes.
4. **Forms** (`/org/forms`): build a dynamic custom form; publish per service.
5. **Queue** (`/org/queue`) + **Display** (`/org/display`): live counters; display access keys (rotate/revoke).
6. **Identities** (`/org/identities`): KYC review; **Finance**: ledger, settlements, payout/refund approvals; reconcile counter shifts.

### 8.3 Operator / Employee (`/employee/login`)
1. Open a shift: request → org-head approval → activate (`/employee/shifts/...`); counter operations are refused until a shift is active (SHIFT-003 guard).
2. **Assisted booking:** search customers, `book-walkin` with payment options (cash collected / pending / at-counter / free / waived-with-reason-approver-evidence / online).
3. Serve the queue: call-next → complete / recall / no-show; multi-stage tokens advance stage-by-stage with the workflow panel.
4. Close the shift: closing summary (cash collected, waived totals) → handover / close request → org reconcile/adjust for shortages/excess.

### 8.4 Customer (`/user/login`, or guest)
1. Register/verify → search organizations → pick service (+slot or day-queue) → dynamic form → book (idempotent; nothing charged until checkout).
2. Track: `/user/track/{token_id}` owner-only live view with workflow progress + waiting-time estimate (timestamped, updates on material queue change).
3. Post-service verified feedback (`/user/feedback`); support cases (`/user/cases`) with threaded replies and resolutions; PDF receipt via purpose-signed reference.

### 8.5 Kiosk (self-service, privacy-first)
1. Org head pairs a device: kiosk requests a pairing PIN (`/public/kiosk/pair-request` → org confirms → `/public/kiosk/pair-exchange` returns the access key).
2. Open `/user/kiosk/{org_id}` on the device: touch wizard (service → counter → details → review) → issue → phone-scannable QR + PDF receipt; repeat prints never duplicate a token.
3. Privacy: 45 s inactivity auto-reset wipes steps and session data; heartbeat (`/public/kiosk/heartbeat`) reports device state; org can revoke the kiosk anytime.

### 8.6 Public Display
1. Registered read-only display access key (per branch; `public_queue:read` only).
2. Open `/public/display-view/{org_id}` on the TV: now-serving + upcoming numbers **only** — no names, phones or other PII; DOM-hardened rendering; live via WebSocket.

### 8.7 AI Booking Assistant *(optional — enable `AI_PROVIDER` or use the mock provider)*
1. Customer opens `/user/assistant` (or "Continue with AI" from a half-filled booking form).
2. Ask in natural language ("book a dentist slot tomorrow morning"); the assistant proposes a plan → **user-gated confirmation** → price/slot revalidated server-side at booking → token issued through the same authoritative path as manual booking (notifications, audit, ledger identical).
3. Prompt-injection defense is surfaced in-conversation; unconfirmed drafts are retained; a booked conversation is terminal (no duplicate charges).

## 9. Known limitations (honest, at Final Closure)

- MFA (AUTH-006), profile avatars (PROF-002..004), shared-field form dedup / employee field-filter views (FORM-006/008), first-login temp-password change (EMP-003), remote display management (DSPM-001..005) and search-UI polish (SRCH-007/008) are **not** implemented — see the reconciled `docs/FEATURE_REQUIREMENTS_MATRIX.md` (14 Missing rows, each with an accurate gap note).
- SEC-009 legal review is a human, pre-production obligation.
- The scanner regex tripwire is not a guarantee (novel secret formats can slip through).
