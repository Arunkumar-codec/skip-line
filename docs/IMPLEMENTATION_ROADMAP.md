# SkipLine: The Smart Queue System — Implementation Roadmap

> **Document Version:** 1.0.0  
> **Date:** October 12, 2026  
> **Status:** Phased Implementation Strategy  
> **Reference Baseline:** [`docs/quirments.docx`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/quirments.docx), [`docs/PROJECT_AUDIT.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/PROJECT_AUDIT.md), [`docs/FEATURE_REQUIREMENTS_MATRIX.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md), [`docs/ARCHITECTURE_PROPOSAL.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/ARCHITECTURE_PROPOSAL.md)

---

## 1. Phased Execution Governance & Gates

SkipLine will be implemented in sequential, well-defined phases according to **Section 25 of the SRS**.

### Core Execution Rules
1. **Zero-Defect Gate Rule:** Do not begin the next phase while the preceding phase has unresolved database migrations, authorization loopholes, data integrity bugs, or automated test failures. UI polish may overlap, but security and source-of-truth invariants must never be deferred.
2. **Backward Compatibility Rule:** All existing database tables and seed records must be safely preserved or migrated using forward-compatible Alembic revisions.
3. **No-Dummy Delivery Standard:** Every page, navigation item, and form delivered in a phase must connect to an authoritative backend API with complete loading, empty, success, and error states.
4. **Explicit Verification:** Each phase concludes with an execution report demonstrating passing unit, API, and integration tests before proceeding to the subsequent phase.

---

## 2. Phase-by-Phase Roadmap

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       PHASED IMPLEMENTATION TIMELINE                        │
├───────────────┬─────────────────────────────────────────────────────────────┤
│ Phase 0       │ Baseline Stabilization & Test Suite Green                   │
│ Phase 1       │ Foundation: Principals, Employees, Branches & RBAC          │
│ Phase 2       │ Catalogue: Multi-Service Counters, Sub-services & Forms     │
│ Phase 3       │ Availability: Schedules, Exceptions, Slots & Atomic Capacity│
│ Phase 4       │ Channels: Kiosk Privacy, Assisted Physical Desks & Shifts   │
│ Phase 5       │ Finance: Immutable Ledger, Cash Reconcile, Refunds & Payouts│
│ Phase 6       │ Token UX: Signed QR Codes, Scanner Check-in & Live Displays │
│ Phase 7       │ Trust: Verified Feedback, Unified Support & KYC Enforcement │
│ Phase 8       │ Hardening: Risk Rules, Performance, E2E Scenarios & Release │
└───────────────┴─────────────────────────────────────────────────────────────┘
```

---

### Phase 0: Baseline Stabilization & Test Suite Green
**Primary Objective:** Stabilize test harness, fix environment-level blockers, achieve 100% test pass rate on existing baseline code without modifying application business logic.

- **Key Deliverables:**
  1. Fix `client` fixture host header in [`tests/conftest.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/tests/conftest.py#L46-L56) (`base_url="http://localhost"` so [`TrustedHostMiddleware`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/main.py#L91-L94) allows test traffic).
  2. Fix [`seed_default_admin`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/admin_service.py#L25-L42) to accept an optional `db: AsyncSession` parameter, preventing live MySQL socket connections during test runs.
  3. Ensure all 65 existing tests pass cleanly (34 unit + 26 API + 2 integration).
  4. Create schema snapshot and baseline Alembic tag.
  5. Set up feature flags configuration in [`app/config.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/config.py).
- **Release Gate 0:** 100% test pass rate across all 65 baseline tests; zero unhandled exceptions; clean Alembic migration state.

---

### Phase 1: Foundation (Principals, Employees, Branches & RBAC)
**Primary Objective:** Establish tenant boundary architecture, introduce employee accounts, physical branches, and fine-grained permissions.

- **Database Changes:**
  - Create `branches` table (id, organization_id, name, code, address, city, state, pincode, is_active).
  - Create `employees` table (id, organization_id, branch_id, name, email, employee_code, password_hash, is_active).
  - Create `roles`, `permissions`, `role_permissions`, and `employee_role_assignments` tables.
  - Create `sessions` table for server-side token invalidation.
- **Backend & APIs:**
  - Implement unified `AuthenticatedPrincipal` dependency in [`app/dependencies.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/dependencies.py).
  - Implement `require_permission(perm: str)` dependency.
  - Add employee authentication routes (`POST /auth/employee/login`).
  - Add branch management APIs (`/org/branches`).
  - Add employee management APIs (`/org/employees`).
- **User Interface:**
  - Create dedicated Employee Login page and portal navigation.
  - Update Organization Head sidebar with Branches and Staff management tabs.
  - Add Branch selection dropdown on relevant forms.
- **Release Gate 1:** Tenant-isolation automated tests pass; employees cannot access organization-head views; permission checks strictly enforced.

---

### Phase 2: Catalogue (Multi-Service Counters, Sub-services & Forms)
**Primary Objective:** Support complex service hierarchies, dynamic booking forms, and many-to-many counter service capabilities.

- **Database Changes:**
  - Create `counter_services` many-to-many junction table ([`ARC-002`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l36)).
  - Create `sub_services` table linked to parent `services`.
  - Create `custom_forms`, `form_fields`, `form_options`, `form_conditions`, `token_form_snapshots`, and `token_form_answers` tables.
  - Create `user_identities` table for customer identity verification.
- **Backend & APIs:**
  - Migrate counter routing logic in [`token_service.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/token_service.py) to use `counter_services`.
  - Implement custom form builder service and validation engine.
  - Add form versioning and JSON snapshotting on booking confirmation.
- **User Interface:**
  - Organization panel: Multi-service selector on Counter configuration modal.
  - Organization panel: Visual drag-and-drop / field builder interface for custom service forms.
  - Customer booking modal: Dynamic form rendering based on selected service.
- **Release Gate 2:** Tokens can be routed to counters based on multiple assigned services; dynamic form data persists correctly in immutable snapshots.

---

### Phase 3: Availability (Schedules, Exceptions, Slots & Capacity)
**Primary Objective:** Eliminate uncontrolled booking rushes with calendar-based operating schedules and atomic slot capacity reservation.

- **Database Changes:**
  - Create `weekly_schedules` (operating hours per day of week).
  - Create `schedule_exceptions` (holiday closures, emergency schedule changes).
  - Create `time_slots` (slot date, start_time, end_time, capacity, booked_count).
  - Create `capacity_rules` table.
  - Add `organizations.timezone` (IANA identifier, server default `Asia/Kolkata`) and nullable `tokens.slot_id` (day-queue tokens stay `NULL`); see migration `0005`.
- **Backend & APIs:**
  - Slot generator background utility.
  - Atomic slot booking with database concurrency locking (`SELECT ... FOR UPDATE` or optimistic versioning).
  - Enhanced organization discovery API supporting distance, pincode, service filter, and available slot dates.
- **User Interface:**
  - Organization panel: Weekly operating hours & holiday calendar manager.
  - Customer booking flow: Interactive date picker and time-slot selector.
- **Design Clarifications (Oct 28, 2026):** Binding design decisions and data-model constraints for this phase are recorded in [`docs/OPEN_DECISIONS.md`](OPEN_DECISIONS.md) §4 (Decisions 10-15): hybrid day-queue/slot capacity model with day-queue as default (`queue_mode` per counter; §25 item 8), schedule scoping via `(scope_level, scope_id, organization_id)` with service > counter > branch > org precedence (SCH-002), UTC storage + IANA timezone per organization and branch (SCH-009), guarded atomic `UPDATE ... WHERE booked_count < capacity` as the portable reservation mechanism, and payment-reservation expiry hooks deferred for Phase 5 wiring (SCH-006).
- **Release Gate 3:** Concurrency stress test proves that slot capacity cannot be oversold under simultaneous concurrent requests.
- **Completion record (development-complete, October 29, 2026):** All Phase 3 deliverables shipped and tested (full suite **363/363 green**; tasks logged in [`docs/FEATURE_REQUIREMENTS_MATRIX.md`](FEATURE_REQUIREMENTS_MATRIX.md)):
  - *Data model:* `weekly_schedules`, `schedule_exceptions`, `time_slots`, `capacity_rules`, `organizations.timezone`, `branches` geo + timezone, `tokens.slot_id`/`booking_source`, `schedule_change_previews` — migrations `0005`–`0008`.
  - *Backend:* schedule resolution (SCH-002 precedence, SCH-003 exceptions, SCH-008 states), idempotent slot generation (the "slot generator utility" is the documented lazy `ensure_slots_for_date`/`generate_slots_for_date` engine reused by booking, calendar, ladder and change previews — no background service; OPEN_DECISIONS §4 discovery notes), atomic guarded-UPDATE slot reservation (portable `SELECT ... FOR UPDATE` alternative embraced by Decision 10/§4), capacity-rule enforcement engine (SCH-004), same-day cutoff (SCH-005), SCH-007 impact workflow, timezone rendering + public slot ladder (SCH-009), discovery filters (Decision 15 scope: distance/pincode/service/slot-date).
  - *UI:* organization-panel schedule manager (weekly hours + holiday calendar; Task 10). Customer booking UX (date picker / time-slot selector screens) is **deferred to Phase 9** per Decision 15 — the selection surface exists as backend APIs (`GET /public/organizations/{id}/slot-dates`, `GET /public/organizations/{id}/slots`).
  - *Release Gate 3 status:* oversell is proven impossible under 6-way concurrent booking on the test backend (guarded conditional UPDATE, two-connection SQLite file-DB stress test); **MySQL InnoDB locking execution remains a deployment gate** — not yet validated, not claimed.
- **Outstanding follow-ups recorded (not resolved):** OPEN **Decision 16** (cancellation/capacity release — blocks the SCH-005 cancel/reschedule cutoff leg and the SCH-007 reschedule-execution leg); refund leg and payment-hold expiry sweeper are Phase 5 (Decisions 11/14); customer UX phase 9 (Decision 15); MySQL validation for migrations `0005`–`0008` incl. InnoDB concurrency (deployment gate).

---

### Phase 4: Channels (Kiosk Privacy, Assisted Physical Desks & Shifts)
**Primary Objective:** Unify physical walk-in operations with online queues while protecting walk-in guest privacy.
**Completion status (recorded October 30, 2026):** **Phase 4 is development-complete.**
- **SHIFT-001/002:** Daily counter shift request (`OPEN_REQUESTED`), head approval (`APPROVED`), activation (`ACTIVE`).
- **SHIFT-003:** Active-shift authorization gate enforced on all employee desk operations (call next, actions, assisted bookings).
- **SHIFT-004:** Shift pause (`PAUSED`), resume (`ACTIVE`), handover, formal closure, and automatic pause on logout.
- **SHIFT-005/006:** Shift closing summary, cash variance computation, head reconciliation review (`reconcile`: approve, request_correction, record_discrepancy, escalate) and authorized adjustment (`adjust`).
- **PHY-001..004:** Assisted physical desk walk-in booking, customer lookup, explicit payment states (`CASH_COLLECTED`, `WAIVED`, `FREE`, `PAY_AT_COUNTER`), waiver reason & supervised evidence verification.
- **KSK-001..006:** Kiosk pairing PIN request (Open Decision 7), org confirmation, PIN exchange for API key, heartbeat monitoring, device revocation, 20s privacy countdown with memory flush, 45s inactivity auto-clear, and idempotency (`client_reference`) token deduplication.
- **Release Gate 4:** Fully validated. 21/21 Phase 4 API integration tests passing green.

- **Database Changes:**
  - Create `counter_shifts` table (counter_id, employee_id, shift_date, status: `CLOSED`, `OPEN_REQUESTED`, `APPROVED`, `ACTIVE`, `PAUSED`, `CLOSE_REQUESTED`, `RECONCILING`).
  - Create `kiosk_devices` table with device activation credentials (migration `0010`).
  - Extend `payments` and `tokens` with waiver evidence, client reference idempotency, and counter_shift tracking (migrations `0010`, `0011`).
- **Backend & APIs:**
  - Employee shift state machine: Open request -> Head approval -> Active desk -> Pause / Resume / Handover -> Close -> Reconcile -> Adjust.
  - Assisted physical counter booking API (`POST /employee/tokens/book-walkin`).
  - Kiosk pairing, heartbeat, revocation and device APIs.
- **User Interface:**
  - Employee Counter Operator station with assisted booking drawer and shift lifecycle modals (`templates/employee/dashboard.html`).
  - Kiosk page update: **20-second automatic privacy reset countdown** with memory flush and **45-second inactivity reset** (`templates/public/kiosk.html`).
  - Organization Head dashboard: Shift approval, active desk monitoring, reconciliation review, adjustments (`templates/org/shifts.html`) and Kiosk device management (`templates/org/kiosks.html`).
- **Release Gate 4:** Kiosk automatically resets privacy without leaving previous visitor data; employees cannot operate counters without approved shift.

---

### Phase 5: Finance (Immutable Ledger, Cash Reconcile, Refunds & Payouts)
**Primary Objective:** Institutional-grade financial integrity with double-entry ledgering and physical cash drawer reconciliation.

- **Database Changes:**
  - Create `ledger_entries` append-only table ([`LED-001`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l223)).
  - Create `cash_sessions` table (starting_float, collected_cash, counted_cash, discrepancy, status).
  - Create `refunds` table with reason, approval audit, and gateway refund ID.
  - Create `settlements` and `payouts` tables.
- **Backend & APIs:**
  - Double-entry accounting service for all Razorpay and cash transactions.
  - Cash shift close reconciliation workflow: Cash counting -> Discrepancy flagging -> Head verification.
  - Razorpay webhook handler with idempotency and signature validation.
  - Admin financial dashboard: Payout hold, fee configuration, and dispute management.
- **User Interface:**
  - Employee shift close modal with physical cash denomination counter.
  - Organization Head Payments tab: Ledger journal view, cash reconciliation reports, payout history.
  - Admin panel: System-wide revenue, gateway escrow balance, and payout releases.
- **Release Gate 5:** Financial reconciliation test passes: total gateway debits + cash collections exactly equal organization earnings + platform fees.

#### Completion status (October 31, 2026) — COMPLETE (all FIN legs closed)

- **Implemented & tested** (full suite **428 passed / 0 failed**, twice consecutively; Phase 5 evidence):
  - *Ledger (LED-001..004):* immutable double-entry ledger (`ledger_entries` via migration `0012`; `models/finance.py`; `app/services/ledger_service.py` — balanced `post_double_entry`, `post_adjustment_entry` with reason/approval, `get_ledger_totals` derived-from-ledger-only). 12 unit tests in `tests/unit/test_ledger_and_finance.py`.
  - *Refunds (PAY-003/PAY-004):* idempotent lifecycle with `idempotency_key` UNIQUE, auto-approval endpoint and approve/reject workflow (`app/services/refund_service.py`, `app/routers/payments.py`, `app/routers/org.py`); reversal entries posted; payment becomes `partially_refunded`/`refunded`.
  - *Webhooks (PAY-003..005):* HMAC-SHA256 raw-body signature verification, `webhook_events.event_id` UNIQUE replay-safety, single-confirmation rule, gateway→revenue ledger charge, hold promotion (`app/services/webhook_service.py`, `POST /payments/webhook`).
  - *Cash (CASH-001..003):* cash sessions with denominations + discrepancy states (matched/shortage/excess/pending_verification) and SHIFT-005 head reconciliation tie-in (`app/services/cash_service.py`, `app/services/shift_service.py`); breakdown reporting endpoint (`/org/api/finance/reports`) + org UI (`templates/org/payments.html`, `templates/org/reports_revenue.html`).
  - *Payouts & bank accounts (FIN-001/FIN-002):* payout scheduling, hold/release with mandatory reason + audit, guarded disbursement posting the payout double entry; bank-account update with 72-hour security hold, owner notification and admin verification (`app/services/payout_service.py`, `app/services/bank_account_service.py`).
  - *Risk flags (FIN-003/FIN-004):* proportional flags (unusual amount, refund spikes, cash discrepancy) with resolve/dismiss requiring notes + audit (`app/services/financial_risk_service.py`).
  - *FIN-001 closure (Oct 31 2026):* organization **payment-method policy** (nullable JSON `organizations.allowed_payment_methods`, migration `0013`, admin-only write with audit, NULL = unrestricted) enforced at **both** acceptance points — online Razorpay order creation and counter walk-ins (online walk-ins validated as `razorpay`) — plus **paid-booking pause/resume** reusing `TokenStatus.ON_HOLD` (capacity consumed, pause ≠ cancel per Decision 16) with mandatory reason, full audit, and an engine-level guard in `perform_token_action` that blocks operator `call`/`progress`/`complete` on paused tokens from every endpoint. Admin API (`/admin/api/organizations/{org_id}/payment-policy`, `/admin/tokens/{id}/pause|resume`), org read view, and a "Payment Control" admin-UI tab.
  - *FIN-003 closure (Oct 31 2026):* the six missing rules — unusual **volume** (≥100 payments/24h, provisional), **duplicate references** (`razorpay_order_id` + `client_reference` reuse, org-scoped), **after-hours activity** (org-timezone resolution via schedule_service; skipped for tenants without published schedules), **waiver irregularity** (every waiver surfaced for review), **price changes** (active-token price snapshots, base + sub-service), **payment-token mismatch** (org mismatch / terminal-token confirmation / multiple successful payments / amount-vs-snapshot) — wired into payment verification, the webhook confirm branch, walk-in booking and catalogue price edits. `flag_risk` gained an additive `commit=False` mode so evaluations never break booking/confirmation atomicity.
  - *Admin financial management UI:* `GET /admin/finance` + `templates/admin/finance.html` — payout review/hold/release/disburse, risk-flag review, bank-verification queue, ledger-derived overview; backed by `/admin/api/finance/*` endpoints (`app/routers/admin.py`); sidebar entry in `templates/shared/admin_sidebar.html`. 7 tests in `tests/api/test_admin_finance_ui.py`.
- **Migration validated (MySQL 8.0.46, disposable database — dropped after validation):** `alembic upgrade head` applies all of `0001`→`0013` on real InnoDB (0013 adds the FIN-001 policy/hold columns, verified up + full downgrade back to 0012 with zero leftover columns); full `downgrade base` also applies after four genuine MySQL fixes (FK-before-index/column drop order in migrations `0004`, `0005`, `0009`, `0010`; plus `migrations/env.py` now builds the async `aiomysql` engine instead of the sync `pymysql` URL — Alembic could not run against MySQL at all before). All Phase 5 tables verified InnoDB/utf8mb4 with FKs, composite indexes and idempotency UNIQUES (`webhook_events.event_id`, `refunds.idempotency_key`). InnoDB two-connection probe of the guarded `UPDATE ... WHERE booked_count + held_count < capacity` primitive passed (row-lock wait + guard re-evaluation → no overbooking; also clears the Phase 3 InnoDB deployment-gate concern).
- **Remaining open items (recorded, not silently dropped):**
  - Settlements: not implemented — the settlement model is an **Open Decision** (MASTER_REQUIREMENTS §25 item 12); the `settlements` table remains unused.
  - Fee configuration: **not implemented and not required** by the authoritative requirements (MASTER_REQUIREMENTS has no admin fee-configuration feature; CASH-003's platform-fee reporting remains ledger-derived).
  - FIN-003 volume/waiver thresholds are PROVISIONAL (OPEN_DECISIONS Decision 18) pending a business decision — no values are stated in the authoritative requirements.
- **Gateway debit/cash vs. earnings reconciliation:** covered at the ledger level — every charge, refund, payout, cash session and adjustment posts balanced double entries, and totals reconcile cash vs. gateway splits in reports; the formal Release Gate reconciliation assertion is executed at phase-sign-off (see Release Gate 5 report).

---

### Phase 6: Token UX (Signed QR Codes, Scanner Check-in & Live Displays)
**Primary Objective:** Fix real-time WebSocket distribution, secure QR codes against enumeration, and deliver customizable TV displays.

- **Backend & APIs:**
  - **Connect WebSocket Broadcast:** Invoke [`broadcast_queue_update`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/websocket/broadcaster.py#L15-L38) on all state changes in [`token_service.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/token_service.py) ([`QUE-005`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l203)).
  - Implement cryptographically signed QR generator using HMAC-SHA256 ([`QR-002`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l259)).
  - Add authorized QR scanner check-in endpoint (`POST /employee/tokens/scan`).
  - Add Redis Pub/Sub backend for multi-worker WebSocket scalability.
- **User Interface:**
  - Upgraded PDF receipt layout with QR code, service details, and appointment slot.
  - Employee camera-based QR scanner module in operator portal.
  - Public TV Display Screen update: Fullscreen multi-counter grid, audio bell chime on token call, customizable ticker banner.
- **Release Gate 6:** Plaintext token ID URL enumeration is impossible; token calls instantly trigger live TV display updates over WebSockets without page reload.

- **Remediation (November 1, 2026, post-`f2a267c`):**
  - **Migration chain ↔ ORM drift resolved with guarded migration `0015_schema_sync_orm_reconcile`** (revision `0015`, down_revision `0014`). Root cause: hand-authored migrations 0001–0003 defined shapes the ORM never used; the functional break was `sessions` (legacy integer-PK/`jti` table could not accept real logins — violating the Phase 0 "Zero-Defect Gate Rule" migration clause for Alembic-built databases). 0015 is fully shape-aware and data-preserving: `sessions` legacy→ORM is a full row-copy rebuild (`jti`→`id`, `last_active_at`→`last_activity_at`) mirrored on downgrade; `audit_logs` actor_* columns are backfilled from principal_*/status and text pre-truncated before narrowing; `role_permissions` legacy rebuild raises (never guesses) when data-bearing. MySQL specifics handled in-place (FK-backing indexes RENAMED not dropped to avoid error 1553; column defaults recaptured before MODIFY-based alters).
  - **Verification (disposable MySQL DB `skipline_synccheck`; real `skipline` DB untouched):** fresh 0001→0015 applies; `alembic check` = *"No new upgrade operations detected."* (was 165 drift events); upgrade→downgrade→re-upgrade cycle passes; the app boots on the purely Alembic-built DB with pre/post-boot schema snapshots byte-identical (startup `create_all` proven a no-op); a real login lifecycle (org register → admin approval → login with JWT `jti` = session `id` → authorized call → `last_activity_at` advance → logout revocation) passes end-to-end — the "Explicit Verification" rule is satisfied for this gate.
  - **Hardening:** public display page's live WebSocket payload no longer rendered via `innerHTML` (DOM `textContent` construction, appearance unchanged — tests/unit/test_display_html_hardening.py); Redis URL credentials never logged (`safe_redis_endpoint()` + `redact_redis_credentials()` at all three log sites in `app/websocket/bus.py` — tests/unit/test_redis_bus.py +5).
  - **Real-Redis Pub/Sub evidence: PENDING** — `tests/integration/test_redis_real.py` exists but skips cleanly here (no reachable Redis server / no `redis` package); the fake-broker unit suite covers the bus logic without Redis. Point `REDIS_TEST_URL` at any instance to run it.
  - Full suite after remediation: **524 passed / 4 skipped** (skips = the 4 Redis-gated integration tests; 513 baseline + 11 new).

---

### Phase 7: Trust (Verified Feedback, Unified Support & KYC Enforcement)
**Primary Objective:** Deliver verified customer reputation, end-to-end customer support ticketing, and tiered organization KYC verification.

- **Database Changes:**
  - Create `feedback` and `feedback_responses` tables.
  - Create `support_cases`, `case_participants`, `case_messages`, and `case_attachments` tables.
  - Create `knowledge_solutions` table.
  - Expand `organization_verifications` with tiered statuses and expiry dates.
- **Backend & APIs:**
  - Token-verified feedback submission (only allowed after token status is `COMPLETED`).
  - Unified support case workflow for User-Org, User-Admin, and Org-Admin inquiries.
  - KYC verification workflow with document review, rejection reasons, and appeal filing.
- **User Interface:**
  - Customer portal: Post-service feedback rating prompt.
  - Support Case messaging interface across all three portals (Customer, Organization, Admin).
  - Admin panel: Dedicated KYC Verification Queue with document preview and approval controls.
- **Release Gate 7:** Customers can only rate completed tokens; cross-tenant support case leakage is impossible under automated tests.

---

### Phase 7 Implementation Report (November 3, 2026)

**Status: implemented end-to-end and Gate 7 satisfied on the automated evidence available in this environment.** No staging/commit/push performed (separate user approval required).

#### Database (migration `0016_trust_feedback_support_kyc`, revision `0016` / down_revision `0015`)
- Eight new tables: `feedback`, `feedback_responses`, `support_cases`, `case_participants`, `case_messages`, `case_attachments`, `knowledge_solutions`, `organization_kyc` (complete Phase 7 table set).
- Additive expansion: `organization_verifications` (+`document_number`, +`expires_at` - ORGKYC-001/003 per-document identity and expiry) and `branches` (+`kyc_verified`, +`kyc_verified_at`, +`kyc_verified_by` FK - ORGKYC-004 branch-level verification).
- Characteristics: fully additive, lossless, MySQL-8-first DDL (no ENUM ALTERs - status columns are guarded plain Strings per the standing Phase 7 decision); batch-safe on SQLite (`branches` column+FK work uses `batch_alter_table`; turns out to be the only SQLite-viable path for FK adds/drops); guarded `_table_exists` branches so sparse/partial chain states degrade gracefully.
- **Verification:** `stamp 0015 -> upgrade 0016 -> downgrade -> re-upgrade` roundtrip on a disposable SQLite DB (all 8 tables created/dropped; a pre-existing `branches` row survives BOTH directions untouched) **AND a real MySQL 8.0.46 full cycle on the disposable `skipline_synccheck` DB: `upgrade 0015 → 0016` → `downgrade 0016 → 0015` → re-`upgrade` → `alembic check` = "No new upgrade operations detected" (zero ORM drift — two initial findings, a migration-only `uq_case_participants` constraint and a VARCHAR-vs-Enum mismatch on `feedback.moderation_status`, were fixed in the migration and re-verified)**. Standalone regression: `tests/unit/test_migration_0016.py`.

#### Services & APIs (`app/routers/trust.py` - 50 handlers, plus `app/services/{feedback,support,knowledge,kyc,attachment}_service.py`)
- **Verified feedback (FDB):** complete-token + owner-only submission over `GET/POST /user/api/tokens/{token_id}/feedback`; one row per token (unique token_id index) with REMOVED-row re-activation on resubmission; org respond/report (tenant-scoped, no edit/delete surface anywhere); five-state moderation with reason/bookkeeping and audited `feedback.moderation.*` events; public reviews (`/public/api/organizations/{org_id}/reviews` + `/summary`) show published-only content with verified badge, month/year label and anonymous display ("Verified customer") and never emit token ids, contacts or identity data.
- **Unified support (SUP/CASE):** one case domain for all seven case types with per-principal authorization; CASE-002 server-side binding validation (foreign/unknown token, payment, feedback ids rejected; tenant derived from the principal); SC- prefixed stable references; participant isolation (strict `get_case_for_participant` gating; lazily provisioned admin participants); threaded messages with internal-note/admin-only visibility and human-review-gated sensitive drafts; private, unguessable-named, access-logged attachments (`attachment_service`); server-side status machine audited per transition; resolution flow accept/reject -> RESOLVED/WAITING_FOR_PARTY with reopen counters; closed cases refuse new messages (409).
- **Knowledge solutions (KB):** org templates + admin global templates, usage counting, suggestion flow into cases where sensitive/financial text requires admin approval before parties can see it.
- **Organization KYC (ORGKYC):** org-level application record (`organization_kyc`, one per org) with the full 12-state taxonomy and transition map; configurable per-category document requirements (`DEFAULT_DOC_REQUIREMENTS` + per-org `Setting('kyc_doc_requirements')` override); document upload/download as private access-logged artifacts with masked listing, admin-only expired-document retention purge (`purged://` tombstones); review queue with missing/expired/duplicate-document indicators; approve/more-info/reject/restrict/suspend/block/reinstate transitions with history ledger; approved/restricted/suspended states feed enforcement.
- **ORGKYC-005 enforcement wired into the booking engine:** `token_service.book_token` (both the day-queue and slot-reservation paths) calls `kyc_service.assert_booking_allowed` - suspended/expired/blocked orgs cannot book; restricted / more-info-required orgs can book free tokens but not paid services (`service.price > 0`). Legacy orgs without a KYC row that already hold `status=approved` are grandfathered APPROVED (Decision 21).
- All authorization is server-side; routers map LookupError -> 404, ValueError -> 409, PermissionError -> 403; enum columns serialize via `.value` (no raw enum leaks in JSON).

#### User Interface (server-rendered, established pattern)
- Customer portal: post-service feedback prompt on completed tokens; support-case inbox, thread (messages, attachments, resolution accept/reject, reopen).
- Org portal: feedback view (respond/report), case inbox + thread, KYC application + document upload + status/appeal page.
- Admin panel: feedback moderation list, case monitor with transitions/approvals, knowledge templates, KYC queue (indicators) + review desk (documents, history, actions, appeal decisions).
- New templates: `templates/{user,org,admin}/{feedback,cases,kyc,knowledge,...}` (9 files) + sidebar entries in all three shared sidebars.

#### Tests (32 focused Phase-7 cases; all green)
- `tests/unit/test_feedback_domain.py` (8), `tests/unit/test_support_cases.py` (8), `tests/unit/test_kyc_enforcement.py` (9), `tests/unit/test_migration_0016.py` (1), `tests/api/test_phase7_trust_api.py` (6), shared factories `tests/phase7_helpers.py`.
- **Full regression at delivery: 556 passed / 4 skipped (the 4 Redis-gated integration tests), zero failures.** Baseline before Phase 7 was 524/4; +32 Phase 7 tests.
- Suite-stability fix included: date-hardened the Phase 3 scheduling/slot test constants (they hardcoded Nov 2-3, 2026 calendar dates that rotated into the past overnight - `BOOK_DAY`/`SLOT_DAY`/`MONDAY` are now computed future weekdays; no product code touched).

#### Gate 7 evaluation
> Customers can only rate completed tokens: **SATISFIED** - enforced in `feedback_service.submit_feedback` + API tests (`tests/api/test_phase7_trust_api.py`, forbidden for waiting tokens, non-owners, repeat submissions).
> Cross-tenant support case leakage is impossible under automated tests: **SATISFIED** - participant gating proven cross-tenant at unit level and API level (403 for the outsider org and stranger user; MUST-level pass).
- **Remaining deployment-gate items (carried, not blockers for the code gate):** (1) ~~execution of migration `0016` plus `alembic check` against the disposable MySQL DB (`skipline_synccheck`)~~ **DONE (Nov 3, 2026): MySQL 8.0.46 was reachable locally (no Docker needed) — upgrade → downgrade → re-upgrade → `alembic check` = "No new upgrade operations detected" (zero drift; the two initial findings were fixed in the migration itself, see the re-verified cycle below);** the full 0001..0015 chain on SQLite is still blocked by the pre-existing 0015 `create_unique_constraint` limitation documented in OPEN_DECISIONS; (2) guest-token feedback remains excluded (Decision-recorded scope cut); (3) malware scanning for KYC/case uploads is not implemented (ORGKYC-007 partial, row marked Incomplete).

---

### Phase 8: Hardening (Risk Rules, Performance, E2E Scenarios & Release)
**Primary Objective:** Production security hardening, performance tuning, full E2E scenario validation, and zero-dummy UI audit.

- **Key Activities:**
  - Run comprehensive security audit: SQL injection, XSS, CSRF verification, rate-limit testing.
  - Implement automated financial and booking anomaly risk flags (`risk_flags`).
  - Perform database query optimization and composite index verification on hot queue paths.
  - Execute complete role-based end-to-end scenarios (Customer, Kiosk Guest, Employee, Org Head, Super Admin).
  - Conduct thorough dead-link and empty-state audit across all panels.
  - Finalize production Docker Compose, Nginx SSL configuration, and database backup routines.
- **Release Gate 8:** Zero unresolved high or critical defects; all 193 requirements verified in matrix; all E2E scenario tests passing; release readiness sign-off.

---

### Phase 9: Booking Continuity & Configurable Workflows (November 4, 2026 — delivered)
**Primary Objective:** complete MASTER §10 (configurable multi-stage service workflows), §7/§14/§15 (agentic AI booking assistant with prompt-injection defense), guest-receipt auth hardening (Workstream A — closed out the Phase 9A interim work), manual↔AI continuity (Workstream D) and Workstream E integration over payments, kiosk, walk-ins, tracking, receipts, notifications and audit, Phase 0–8 untouched.

#### Workstream A — receipt/QR security close-out (`tests/api/test_guest_receipt_security.py`, 30 tests)
- `/qr_codes` static mount REMOVED; `/user/track/{token_id}` owner-only; kiosk own-QR via authorized `GET /tokens/{id}/qr?ref=`; nginx log format `$uri`; purpose-separated signed references (`PURPOSE_RECEIPT`); guest PII suppression in tracking.

#### Workstream B — configurable multi-stage workflows (`app/services/workflow_service.py`, `app/routers/workflow.py`, migration `0018`)
- org config CRUD (`/org/workflows*`, per-service workflows + `/org/workflows/list` JSON), booking auto-start hook, employee stage ops (`/employee/tokens/{id}/workflow*`: advance/skip/recall/complete/cancel + history), CAS-race safety (409), tenant isolation, tracking progress, `finalize_for_token_status` ledger close on complete/cancel/skip, B15 legacy single-stage guarantee.
- Org UI: `templates/org/workflows.html` page + sidebar entry; employee dashboard Stage panel (`loadStagePanel`, recall with reason prompt); customer tracking renders the workflow block on `/public/track`.
- Tests: `tests/api/test_workflows.py` (19 tests incl. the page-render + deterministic stale-CAS + 409-mapping), `tests/unit/test_migration_0018.py` (up/down/re-upgrade roundtrip).

#### Workstream C — agentic AI booking (`app/services/ai_provider.py`, `app/services/ai_assistant_service.py`, `app/routers/ai.py`)
- LLM reasoning is SEPARATE from authoritative backend tools; strict-JSON action protocol; tool registry delegating to `user_service`/`schedule_service`/catalogue/`token_service.book_token`.
- USER-GATED confirmation (endpoint param `confirmed`, plus explicit `POST /ai/conversations/{id}/book`); price/slot revalidation at final booking; duplicate-safe (`ai_conv_{id}` idempotency + booked-status terminal guard); notification + audit integration.
- Graceful unavailability; one-repair-round protocol; bounded history; prompt-injection defense surfaced in conversation and tool content.
- Tests: `tests/api/test_ai_assistant.py` (20 tests) with a SCRIPTED mock provider (no paid external calls by design).

#### Workstream D — manual↔AI continuity
- `ai_conversations` state (mode/status/context/draft/booking); D1 manual→AI carry-in (`/ai/session` + booking-form "Continue with AI" link + `/user/assistant` page), D2 AI→manual prefill (`switch-mode`), D3 booked-conversation terminality, D4 unconfirmed drafts retained, D5 live revalidation, D6 owner-scoped state endpoint; UI at `templates/user/assistant.html`.

#### Workstream E — integration guarantee
- Workflow + AI code routed through the SAME `token_service.book_token` / `perform_token_action` paths — payments (nothing charged until checkout), kiosk/walk-in channels, tracking (`workflow` key optional — consumers unaffected), receipts, notifications (`notify_token_created`) and audit (`audit_service.log_audit_event`) all continue through their Phase 0-8 implementations. Full-suite regression run verifies the surfaces.

- **Full-suite regression at Phase 9 delivery: 759 passed / 0 failed (Workstream B close-out run), with Workstream C/D suites added afterwards and a final full run recorded at delivery.**

