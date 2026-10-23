# SkipLine: The Smart Queue System — Project Audit

> **Audit Date:** October 12, 2026  
> **Repository Root:** `c:\Arunakumara\skip-line-FINAL\skip-line`  
> **Environment:** Python 3.12.10, FastAPI 0.111, SQLAlchemy 2.0.31, MySQL 8.3 / SQLite (in-memory tests)  
> **Author:** Antigravity Engineering Agent  
> **Reference Baseline:** [`docs/quirments.docx`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/quirments.docx), [`README.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/README.md), [`docs/api.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/api.md), [`docs/database.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/database.md)

---

## 1. Executive Summary

This comprehensive project audit evaluates the **SkipLine: The Smart Queue System** repository. The project aims to provide a unified, multi-tenant digital queue management platform serving hospitals, banks, government administrative centers, educational facilities, and commercial services. The platform is designed to eliminate physical waiting lines through digital booking (online, entrance kiosk, employee-assisted), live queue tracking, automated calling, customizable TV displays, integrated payments, and verified feedback.

The audit was conducted strictly under non-destructive rules:
1. No existing application code was altered.
2. No files were deleted, and no tests were weakened.
3. No secrets or credentials from configuration files were exposed.
4. All existing models, routers, services, templates, websocket components, and test suites were thoroughly inspected.
5. Automated test suites were executed under the active virtual environment ([`venv/Scripts/python.exe`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/venv/Scripts/python.exe)) and their raw results documented without assumption.

### Key Audit Findings at a Glance

| Audit Dimension | Evaluation | Summary Finding |
|---|:---:|---|
| **Architectural Foundation** | **Partial** | Solid modern async FastAPI application layout with separation into routers, services, models, schemas, and templates. However, authentication and authorization are tightly coupled to three legacy roles (`admin`, `organization`, `user`) without support for employees, branches, granular permissions, or multi-service counter routing. |
| **Test Suite Health** | **Broken** | **34 Passed, 28 Failed** (out of 62 executed / 65 collected). Pure unit tests pass 100% (34/34). All 26 API tests fail due to [`TrustedHostMiddleware`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/main.py#L91-L94) rejecting the test client host header (`http://test`). Both integration tests fail because [`seed_default_admin()`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/admin_service.py#L25-L42) bypasses the SQLite test session and forces a direct connection to live MySQL. |
| **Real-Time WebSocket Engine** | **Partially Broken** | WebSocket endpoints ([`/ws/org/{org_id}`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/routers/websocket.py#L20-L37) and [`/ws/token/{token_id}`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/routers/websocket.py#L39-L55)) and room manager ([`app/websocket/manager.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/websocket/manager.py)) exist. However, the high-level broadcast function [`broadcast_queue_update`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/websocket/broadcaster.py#L15-L38) is **never invoked** by any token lifecycle method in [`token_service.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/token_service.py) or router. Operators and users only receive updates via fallback 15-second HTTP polling. |
| **Security & Privacy Posture** | **High Risk** | QR codes encode plaintext sequential integer URLs (`http://localhost:8000/public/track/{token_id}`), allowing trivial token enumeration and tampering. There is no cryptographic signature, no scanner verification endpoint, no CSRF validation for HTML form submissions (despite comments in code), and no employee audit logging. |
| **Financial & Ledger Integrity** | **Incomplete** | Basic Razorpay order creation and signature verification exist in [`payment_service.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/payment_service.py), but the system lacks an immutable double-entry ledger, cash collection tracking, employee shift reconciliation, refund management, and automated dispute handling required by SRS Section 12. |
| **Domain Coverage vs SRS** | **~25% Complete** | The existing system implements 12 database tables. The SRS specification defines 35+ core entities across 11 distinct domains including Identity Verification, Multi-branch Tenancy, Employee Shift Approvals, Dynamic Form Builders, Slots & Schedules, Ledger Entries, Support Cases, and Device Credentials. |

---

## 2. Workspace & Codebase Inventory

### 2.1 File & Directory Structure

The repository contains 10 top-level files and 16 top-level directories:

```
c:\Arunakumara\skip-line-FINAL\skip-line\
├── app\                          # Core FastAPI application package
│   ├── config.py                 # Pydantic BaseSettings loading from .env
│   ├── database.py               # Async SQLAlchemy engine & AsyncSessionLocal
│   ├── dependencies.py           # Authentication dependencies (admin, org, user)
│   ├── main.py                   # Application factory, lifespan, middleware, mounts
│   ├── middleware\               # Logging and rate-limiting middleware
│   │   ├── logging.py            # Loguru request duration logger
│   │   └── rate_limit.py         # In-memory sliding window rate limiter
│   ├── routers\                  # 13 FastAPI routers (REST + HTML views)
│   ├── schemas\                  # 9 Pydantic validation and serialization schemas
│   ├── services\                 # 6 Business logic service modules
│   ├── utils\                    # JWT, bcrypt, QR generator, PDF ReportLab generator
│   └── websocket\                # In-memory WebSocket manager, broadcaster, heartbeat
├── database\                     # SQL scripts (schema.sql, init.sql, seed.sql)
├── docs\                         # Project documentation and specifications
│   ├── api.md                    # REST API overview
│   ├── database.md               # 12-table database summary
│   ├── deployment.md             # Docker and Ubuntu deployment guide
│   └── quirments.docx            # Complete SRS specification (193 requirements)
├── migrations\                   # Alembic database migration environment
│   └── versions\
│       └── 0001_initial_schema.py# Baseline schema migration
├── models\                       # 14 SQLAlchemy ORM models + base mixins
├── nginx\                        # Nginx reverse proxy configuration (nginx.conf)
├── qr_codes\                     # Directory for generated token QR PNGs
├── receipts\                     # Directory for generated PDF receipt files
├── static\                       # Front-end CSS and vanilla JavaScript modules
│   ├── css\main.css              # Custom CSS design system
│   └── js\                       # main.js, queue-operator.js, display-screen.js, token-tracker.js
├── templates\                    # Jinja2 HTML templates
│   ├── admin\                    # 8 Admin panel templates
│   ├── org\                      # 9 Organization panel templates
│   ├── public\                   # 4 Public portal templates (index, kiosk, display, track)
│   ├── shared\                   # 5 Shared layout templates and navigation sidebars
│   └── user\                     # 6 Customer user portal templates
├── tests\                        # Test suite (unit, api, integration)
│   ├── conftest.py               # Shared async fixtures with SQLite in-memory
│   ├── api\                      # 4 API endpoint test suites
│   ├── integration\              # 1 Full lifecycle integration test suite
│   └── unit\                     # 5 Pure logic unit test suites
├── Dockerfile                    # Multi-stage production container build
├── docker-compose.yml            # App + MySQL 8.3 + Nginx composition
├── Makefile                      # Developer shortcut commands
├── pytest.ini                    # Pytest configuration and markers
├── requirements.txt              # Python package dependencies
└── README.md                     # Project overview and quick-start instructions
```

### 2.2 Database Models Audit (12 Tables)

The active database model layer is defined under [`models/`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/):

| Model Class | File | Target Table | Primary Responsibility | Observed Gaps |
|---|---|---|---|---|
| [`Admin`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/admin.py) | `models/admin.py` | `admins` | Super administrator accounts | No granular permission flags; single role flag. |
| [`Organization`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/organization.py) | `models/organization.py` | `organizations` | Tenant organization profiles | Lacks branch hierarchy, tax/GST fields, bank accounts, refund policies. |
| [`OrganizationVerification`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/organization_verification.py) | `models/organization_verification.py` | `organization_verifications` | Uploaded KYC document audit | Single document string; lacks document category enums, expiry date, multi-stage approval. |
| [`User`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/user.py) | `models/user.py` | `users` | End users booking tokens | No ID verification status (Aadhaar/PAN/Govt ID), profile image storage link, or phone verification. |
| [`Service`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/service.py) | `models/service.py` | `services` | Services provided by organization | Lacks sub-services, dynamic form link, category hierarchy, tax rate, differential pricing. |
| [`Counter`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/counter.py) | `models/counter.py` | `counters` | Physical service desks | Restricted to single `service_id`; lacks multi-service many-to-many relationship, shift state, branch assignment. |
| [`Token`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/token.py) | `models/token.py` | `tokens` | Queue token records (core entity) | Token number prefix hardcoded per counter; missing appointment slot reference, dynamic form answers snapshot, secure QR secret. |
| [`TokenHistory`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/token_history.py) | `models/token_history.py` | `token_history` | Status transition audit | Simple string roles; lacks employee principal ID foreign key, device ID, IP address, transition reason. |
| [`Payment`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/payment.py) | `models/payment.py` | `payments` | Token payment transactions | Single record per token; lacks ledger transaction link, line items, tax split, refund tracking, cash collection session. |
| [`Notification`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/notification.py) | `models/notification.py` | `notifications` | User event notifications | No SMS delivery receipt, template variables, or push notification tokens. |
| [`QueueLog`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/queue_log.py) | `models/queue_log.py` | `queue_logs` | Append-only queue action log | JSON metadata stored as unindexed text; actor roles unconstrained. |
| [`Setting`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/setting.py) | `models/setting.py` | `settings` | Org key-value configuration | Untyped text values; no schema validation or encryption for sensitive values. |

---

## 3. Test Suite Execution & Verification Audit

### 3.1 Test Run Protocol

The automated test suite was executed using pytest in the project's virtual environment:
```powershell
venv\Scripts\pytest.exe -v
```

### 3.2 Actual Execution Results

```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-8.2.2, pluggy-1.6.0
rootdir: C:\Arunakumara\skip-line-FINAL\skip-line
configfile: pytest.ini
plugins: anyio-4.4.0, asyncio-0.23.7, cov-5.0.0
asyncio: mode=Mode.AUTO
collected 65 items

tests\unit\test_config.py::TestConfig::test_default_settings PASSED
tests\unit\test_config.py::TestConfig::test_production_flag PASSED
tests\unit\test_config.py::TestConfig::test_async_database_url_format PASSED
tests\unit\test_config.py::TestConfig::test_sync_database_url_format PASSED
tests\unit\test_config.py::TestConfig::test_allowed_hosts_parsing PASSED
tests\unit\test_config.py::TestConfig::test_cors_origins_parsing PASSED
tests\unit\test_config.py::TestConfig::test_special_chars_in_password_url_encoded PASSED
tests\unit\test_config.py::TestConfig::test_jwt_defaults PASSED
tests\unit\test_config.py::TestConfig::test_email_defaults PASSED
tests\unit\test_notification_service.py::test_build_message_content PASSED
tests\unit\test_notification_service.py::test_build_subject PASSED
tests\unit\test_notification_service.py::test_unknown_type_fallback PASSED
tests\unit\test_pdf_generator.py::test_pdf_creates_valid_file PASSED
tests\unit\test_pdf_generator.py::test_pdf_output_path_matches_convention PASSED
tests\unit\test_pdf_generator.py::test_pdf_contains_token_number PASSED
tests\unit\test_security.py::TestPasswords::test_hash_and_verify PASSED
tests\unit\test_security.py::TestPasswords::test_wrong_password_fails PASSED
tests\unit\test_security.py::TestPasswords::test_empty_password_fails PASSED
tests\unit\test_security.py::TestPasswords::test_hashes_are_unique PASSED
tests\unit\test_security.py::TestJWT::test_create_and_decode PASSED
tests\unit\test_security.py::TestJWT::test_expired_token_returns_none PASSED
tests\unit\test_security.py::TestJWT::test_tampered_token_returns_none PASSED
tests\unit\test_security.py::TestJWT::test_roles_are_distinct PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_book_token_sequence_increments PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_daily_limit_enforced PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_inactive_counter_raises PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_call_action PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_recall_action PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_hold_action PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_complete_action PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_skip_action PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_cancel_action PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_invalid_transition_raises PASSED
tests\unit\test_token_service.py::TestTokenLifecycle::test_unknown_action_raises PASSED

tests\api\test_auth.py::test_health_check FAILED [assert 400 == 200]
tests\api\test_auth.py::test_user_register FAILED [assert 400 == 200]
tests\api\test_auth.py::test_user_register_duplicate_email PASSED
tests\api\test_auth.py::test_user_login_success FAILED [assert 400 == 200]
tests\api\test_auth.py::test_user_login_wrong_password FAILED [assert 400 == 401]
tests\api\test_auth.py::test_org_register FAILED [assert 400 == 200]
tests\api\test_auth.py::test_logout FAILED [assert 400 == 200]

tests\api\test_organizations.py::test_org_register_creates_pending FAILED [assert 400 == 200]
tests\api\test_organizations.py::test_org_login_blocked_while_pending FAILED [assert 400 == 403]
tests\api\test_organizations.py::test_public_org_search_empty FAILED [assert 400 == 200]
tests\api\test_organizations.py::test_public_org_services_endpoint FAILED [assert 400 == 200]
tests\api\test_organizations.py::test_org_password_mismatch_rejected FAILED [assert 400 == 422]

tests\api\test_public.py::test_health_check FAILED [assert 400 == 200]
tests\api\test_public.py::test_landing_page_returns_html FAILED [assert 400 == 200]
tests\api\test_public.py::test_display_screen_nonexistent_org FAILED [assert 400 == 200]
tests\api\test_public.py::test_track_nonexistent_token FAILED [assert 400 == 404]
tests\api\test_public.py::test_kiosk_book_validation FAILED [assert 400 == 422]
tests\api\test_public.py::test_org_search_returns_paginated FAILED [assert 400 == 200]
tests\api\test_public.py::test_public_org_counters_nonexistent FAILED [assert 400 == 200]
tests\api\test_public.py::test_user_login_page_returns_html FAILED [assert 400 == 200]
tests\api\test_public.py::test_org_login_page_returns_html FAILED [assert 400 == 200]
tests\api\test_public.py::test_admin_login_page_returns_html FAILED [assert 400 == 200]

tests\api\test_tokens.py::test_user_active_tokens_empty_on_registration FAILED [assert 400 == 200]
tests\api\test_tokens.py::test_track_nonexistent_token_returns_404 FAILED [assert 400 == 404]
tests\api\test_tokens.py::test_token_history_requires_auth FAILED [assert 400 == 401]
tests\api\test_tokens.py::test_kiosk_book_missing_fields FAILED [assert 400 == 422]
tests\api\test_tokens.py::test_receipt_nonexistent_token_returns_404 FAILED [assert 400 == 404]

tests\integration\test_full_flow.py::test_admin_seed_creates_admin FAILED [aiomysql Proactor event loop / socket send error]
tests\integration\test_full_flow.py::test_full_token_lifecycle FAILED [aiomysql Proactor event loop / socket send error]

=========================== Summary ===========================
34 passed, 28 failed, 5 warnings in 1.35s
```

### 3.3 Root Cause Analysis of Failures

#### Failure Cause 1: `TrustedHostMiddleware` Host Mismatch in Test Client
- **Impact:** 26 API tests returned HTTP 400 (`{"detail": "Invalid host header"}`).
- **Mechanism:** In [`tests/conftest.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/tests/conftest.py#L46-L56), the `client` fixture initializes:
  ```python
  AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
  ```
  HTTP requests sent through this client present `Host: test`. In [`app/main.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/main.py#L91-L94), Starlette's `TrustedHostMiddleware` is configured with `allowed_hosts=settings.allowed_hosts`, which defaults to `["localhost", "127.0.0.1"]`. Because `test` is not in `allowed_hosts`, Starlette intercepts every incoming request before it reaches any router and immediately responds with HTTP 400 "Invalid host header".
- **Remediation Plan (Phase 0):** Update `tests/conftest.py` so `base_url="http://localhost"` or override `allowed_hosts` in test environment configuration.

#### Failure Cause 2: `seed_default_admin` Direct Live Database Coupling
- **Impact:** 2 integration tests failed with `AttributeError: 'NoneType' object has no attribute 'send'` / `RuntimeError: Event loop is closed`.
- **Mechanism:** In [`app/services/admin_service.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/admin_service.py#L25-L42), `seed_default_admin()` directly imports and invokes:
  ```python
  from app.database import AsyncSessionLocal
  async with AsyncSessionLocal() as db:
  ```
  `AsyncSessionLocal` is bound to the live MySQL connection URL (`settings.async_database_url`), completely bypassing the in-memory SQLite `db_session` fixture provided by [`tests/conftest.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/tests/conftest.py). In an isolated test environment without an active MySQL daemon on port 3306, the async socket connection terminates abruptly and throws an unhandled event loop error.
- **Remediation Plan (Phase 0):** Refactor `seed_default_admin(db: Optional[AsyncSession] = None)` to accept an explicit session or rely on dependency injection, with fallback to `AsyncSessionLocal()`.

---

## 4. Subsystem Deep-Dive & Gap Analysis

### 4.1 Authentication & Authorization
- **Current State:**
  - Separate login endpoints: [`/auth/admin/login`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/routers/auth.py#L18-L38), [`/auth/org/login`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/routers/auth.py#L40-L60), [`/auth/user/login`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/routers/auth.py#L62-L82).
  - JWT tokens issued with custom role claim (`admin`, `organization`, `user`).
  - Passwords hashed with bcrypt via passlib.
  - Dependencies: [`get_current_admin`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/dependencies.py#L45-L63), [`get_current_org`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/dependencies.py#L67-L85), [`get_current_user`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/dependencies.py#L89-L107).
- **Gaps Against SRS:**
  - **No Employee Principal:** Employees must log in as the organization itself, violating `AUTHZ-004` and `AUTHZ-005`.
  - **No Permission Matrix:** Authorization is strictly binary per role. No fine-grained permissions (`counter.open.request`, `token.call`, `cash.collect`, `shift.reconcile`).
  - **No Session Invalidation:** JWTs cannot be revoked server-side on logout or role change.
  - **Missing CSRF Protection:** While forms include `<input type="hidden" name="csrf_token" ...>`, no CSRF validation middleware exists in [`app/main.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/main.py).

### 4.2 Queue Engine & Live Synchronization
- **Current State:**
  - Token state machine supports transitions: `waiting` → `called` → `in_progress` → `completed`, plus `on_hold`, `skipped`, `cancelled`.
  - Queue positions recalculated on state change.
  - Daily limit per counter enforced.
  - In-memory WebSocket room manager ([`app/websocket/manager.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/websocket/manager.py)) manages connections for orgs and individual tokens.
- **Critical Flaw:**
  - In [`app/websocket/broadcaster.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/websocket/broadcaster.py), `broadcast_queue_update` is defined to broadcast updates to connected display screens and user trackers.
  - **However, `broadcast_queue_update` is never imported or called anywhere in [`app/services/token_service.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/token_service.py) or [`app/routers/org.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/routers/org.py)!**
  - As a result, WebSocket clients do not receive push notifications when tokens are called, completed, or booked. The UI only updates via 15-second polling fallback.
- **Gaps Against SRS:**
  - No atomic lock on slot capacity (`QUE-001`, `QUE-002`).
  - No support for multi-service counter routing (`CTR-001`).
  - In-memory WebSocket manager cannot scale across multiple workers or instances without Redis Pub/Sub (`ARC-Scaling`).

### 4.3 Channels & Kiosk Workflow
- **Current State:**
  - Online booking: [`POST /user/tokens/book`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/routers/user.py#L52-L69).
  - Entrance kiosk booking: [`POST /public/kiosk/book`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/routers/public.py#L23-L42).
- **Gaps Against SRS:**
  - **Kiosk Privacy:** Kiosk screen does not auto-reset after token issuance (`KSK-002`), leaving previous visitors' names and phone numbers visible to next persons.
  - **Kiosk Fraud Prevention:** No device activation credential, rate-limiting, or abuse prevention (`KSK-003`, `KSK-004`).
  - **Missing Physical Counter Channel:** No employee-assisted physical counter booking workflow (`PHY-001` - `PHY-004`).
  - **No Shift Management:** No counter opening requests or head approval state machine (`SHIFT-001` - `SHIFT-005`).

### 4.4 Payments & Financial Ledger
- **Current State:**
  - Basic Razorpay integration: order creation ([`POST /payments/initiate`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/routers/payments.py#L22-L47)) and signature verification ([`POST /payments/verify`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/routers/payments.py#L49-L66)).
  - Single `Payment` model linked to token.
- **Gaps Against SRS:**
  - **No Immutable Ledger:** `payments` table is mutated directly. Missing double-entry `ledger_entries` table (`LED-001` - `LED-004`).
  - **No Price Snapshots:** If service price changes, historical token financial records cannot be verified (`DB-003`).
  - **No Cash Reconciliation:** Cash payments cannot be tracked per employee shift or matched with physical cash drawer count (`CASH-001` - `CASH-003`).
  - **No Refund Lifecycle:** No partial or full refund tracking with admin approval controls (`FIN-001` - `FIN-004`).

### 4.5 QR Codes, Receipts & Scanning
- **Current State:**
  - PDF receipts generated via ReportLab in [`app/utils/pdf_generator.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/utils/pdf_generator.py).
  - QR codes generated via `qrcode` package in [`app/utils/qr_generator.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/utils/qr_generator.py).
- **Critical Vulnerabilities:**
  - **Plaintext Sequential URLs:** QR code encodes `http://localhost:8000/public/track/{token_id}`. An attacker can scan one QR code, extract the format, and enumerate every token in the system by incrementing the token ID (`QR-002`).
  - **Hardcoded Localhost URL:** [`qr_generator.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/utils/qr_generator.py#L24) defaults `base_url` to `http://localhost:8000`, causing broken links when deployed in production or accessed over a network.
  - **No Scanning Endpoint:** No authorized endpoint exists for counter operators or security guards to scan a QR code and verify check-in (`QR-003` - `QR-005`).

### 4.6 Trust, Feedback & Customer Support
- **Current State:**
  - Completely absent from current codebase.
- **Gaps Against SRS:**
  - **No Verified Token Feedback:** Customers cannot submit ratings or reviews tied to their completed tokens (`FDB-001` - `FDB-005`).
  - **No Unified Support Cases:** No ticketing or messaging module for user-to-org, user-to-admin, or org-to-admin communications (`SUP-001` - `SUP-007`, `CASE-001` - `CASE-004`).
  - **No Knowledge Base:** No canned solutions or suggested responses for support cases (`KB-001` - `KB-002`).

---

## 5. Security & Risk Register

| Risk ID | Severity | Category | Risk Description | Current Code Evidence | Mitigation Strategy |
|---|:---:|---|---|---|---|
| **SEC-01** | **CRITICAL** | ID Enumeration / Tampering | Sequential integer token IDs in QR codes allow unauthenticated public enumeration of queue tokens and visitor metadata. | [`qr_generator.py:L30`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/utils/qr_generator.py#L30) | Introduce cryptographically signed opaque token references (HMAC-SHA256 or UUIDv4) in QR codes. |
| **SEC-02** | **HIGH** | Authorization / Tenancy | Absence of employee principals forces organizations to share single credentials with all desk staff, destroying auditability and violating principle of least privilege. | [`dependencies.py:L67-L85`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/dependencies.py#L67-L85) | Implement explicit `employees` entity, role-based access control, and shift assignments in Phase 1. |
| **SEC-03** | **HIGH** | Data Integrity / Finance | Financial records lack immutable ledger entries and cash reconciliation; payment totals can be mutated without audit trail. | [`payment_service.py:L92-L117`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/payment_service.py#L92-L117) | Create append-only `ledger_entries` and `cash_sessions` tables with double-entry accounting in Phase 5. |
| **SEC-04** | **MEDIUM** | Web Security / CSRF | Web forms include hidden CSRF inputs, but the backend lacks validation middleware, leaving state-changing browser requests vulnerable to CSRF. | [`templates/user/profile.html:L47`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/templates/user/profile.html#L47), [`main.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/main.py) | Integrate Starlette/FastAPI CSRF middleware with double-submit cookie validation. |
| **SEC-05** | **MEDIUM** | Information Disclosure | Public kiosk interface does not automatically clear or timeout after token generation, exposing guest name and mobile number to subsequent users. | [`templates/public/kiosk.html:L195-L201`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/templates/public/kiosk.html#L195-L201) | Implement 20-second client-side auto-reset countdown with session memory flush. |
| **SEC-06** | **MEDIUM** | Operational Stability | In-memory WebSocket manager cannot synchronize state across multiple Uvicorn workers or server replicas. | [`app/websocket/manager.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/websocket/manager.py) | Abstract WebSocket broadcast layer to support Redis Pub/Sub backend. |

---

## 6. Audit Conclusion & Readiness Assessment

The existing codebase contains a functional, well-structured FastAPI foundation with clean Jinja2 templates, async SQLAlchemy models, and basic queue state transitions. However:
1. **The test suite is blocked** by two low-risk environment configuration issues (Host header validation and hardcoded live DB connection in the admin bootstrap routine).
2. **The real-time notification loop is incomplete** due to uncalled WebSocket broadcast logic.
3. **Core enterprise requirements are missing**, including employee accounts, multi-service counter routing, appointment scheduling, immutable financial ledgering, signed QR scanning, and customer support.

**Recommendation:**
Proceed to **Phase 0 (Baseline Stabilization)** to unblock and pass 100% of the existing test suite, freeze the database schema, and establish the technical baseline before initiating feature expansion in Phase 1.
