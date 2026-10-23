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
- **Backend & APIs:**
  - Slot generator background utility.
  - Atomic slot booking with database concurrency locking (`SELECT ... FOR UPDATE` or optimistic versioning).
  - Enhanced organization discovery API supporting distance, pincode, service filter, and available slot dates.
- **User Interface:**
  - Organization panel: Weekly operating hours & holiday calendar manager.
  - Customer booking flow: Interactive date picker and time-slot selector.
- **Release Gate 3:** Concurrency stress test proves that slot capacity cannot be oversold under simultaneous concurrent requests.

---

### Phase 4: Channels (Kiosk Privacy, Assisted Physical Desks & Shifts)
**Primary Objective:** Unify physical walk-in operations with online queues while protecting walk-in guest privacy.

- **Database Changes:**
  - Create `counter_shifts` table (counter_id, employee_id, shift_date, status: `CLOSED`, `OPEN_REQUESTED`, `APPROVED`, `ACTIVE`, `PAUSED`, `CLOSE_REQUESTED`, `RECONCILING`).
  - Create `kiosk_devices` table with device activation credentials.
- **Backend & APIs:**
  - Employee shift state machine: Open request -> Head approval -> Active desk.
  - Assisted physical counter booking API (`POST /employee/tokens/book-walkin`).
  - Kiosk rate-limiting and device authentication middleware.
- **User Interface:**
  - Employee Counter Operator station with assisted booking drawer.
  - Kiosk page update: **20-second automatic privacy reset countdown** with memory flush ([`KSK-002`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l157)).
  - Organization Head dashboard: Real-time shift opening requests and approval drawer.
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
