# SkipLine: The Smart Queue System — Open Decisions & Architectural Choices

> **Document Version:** 1.0.0  
> **Date:** October 12, 2026  
> **Status:** Pending User Review & Authorization  
> **Reference Baseline:** [`docs/quirments.docx`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/quirments.docx), [`docs/PROJECT_AUDIT.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/PROJECT_AUDIT.md), [`docs/ARCHITECTURE_PROPOSAL.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/ARCHITECTURE_PROPOSAL.md), [`docs/IMPLEMENTATION_ROADMAP.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/IMPLEMENTATION_ROADMAP.md)

---

## 1. Purpose

This document outlines key technical, architectural, and business decisions that require alignment before executing subsequent phases. For each decision, practical options, tradeoffs, and engineering recommendations are presented to enable informed review.

---

## 2. Key Decisions Matrix

### Decision 1: Principal & Authentication Unification Model
- **Context:** The system currently maintains three separate tables ([`admins`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/admin.py), [`organizations`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/organization.py), [`users`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/user.py)) and needs to introduce `employees` with fine-grained permissions ([`AUTHZ-005`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l51)).
- **Options:**
  - **Option 1A: Separate Dedicated Tables Unified in Application Layer (Recommended)**
    - Keep `admins`, `organizations`, `employees`, and `users` as distinct tables.
    - Resolve requests into a common `AuthenticatedPrincipal` dataclass in [`app/dependencies.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/dependencies.py).
    - *Pros:* Fully preserves existing schema and seed data; zero data migration risk for existing user/admin accounts; clean domain boundaries.
    - *Cons:* Multiple login routers and distinct password hashing columns.
  - **Option 1B: Single Consolidated `principals` Table**
    - Create a unified `principals` table (id, email, password_hash, principal_type) with 1:1 polymorphic foreign keys to profile tables.
    - *Pros:* Single login endpoint and unified session revocation.
    - *Cons:* High migration risk; breaks existing foreign keys and seed scripts.
- **Recommendation:** **Option 1A**. Maintain separate domain tables and unify authentication claims via the `AuthenticatedPrincipal` abstraction.

---

### Decision 2: Real-Time Synchronization Engine & Infrastructure
- **Context:** The WebSocket broadcast pipeline is currently disconnected ([`QUE-005`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l203)), and the in-memory WebSocket manager cannot scale across multiple Uvicorn workers.
- **Options:**
  - **Option 2A: Hybrid In-Memory with Optional Redis Pub/Sub (Recommended)**
    - Implement a unified broadcaster interface that defaults to in-memory room management for local development / single worker, and automatically switches to Redis Pub/Sub when `REDIS_URL` is configured in `.env`.
    - *Pros:* Works out of the box for lightweight local dev without external services; seamlessly scales across multiple container replicas in production.
    - *Cons:* Requires abstracting the broadcaster with two interchangeable backends.
  - **Option 2B: Redis-Only Mandatory Dependency**
    - Require Redis 7 in all environments including local development.
    - *Pros:* Single code path.
    - *Cons:* Adds an external infrastructure requirement for running local tests.
- **Recommendation:** **Option 2A**. Implement the hybrid adapter with Redis auto-detection.

---

### Decision 3: Multi-Service Counter Migration Strategy
- **Context:** Counters are currently hardcoded to a single `service_id` foreign key ([`models/counter.py:L26`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/models/counter.py#L26)). Requirement [`ARC-002`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l36) mandates many-to-many counter-service assignments.
- **Options:**
  - **Option 3A: Backward-Compatible Non-Breaking Migration (Recommended)**
    - Create `counter_services` junction table.
    - Write an Alembic migration that reads all existing `counters.service_id` records and populates initial `counter_services` rows.
    - Keep `counters.service_id` nullable as a legacy fallback column, while switching active queue matching logic to query `counter_services`.
    - *Pros:* Zero downtime; existing seed data remains valid; safe rollback path.
    - *Cons:* Temporary column redundancy until Phase 8 cleanup.
  - **Option 3B: Immediate Hard Replacement**
    - Drop `counters.service_id` and enforce `counter_services` immediately.
    - *Pros:* Clean schema.
    - *Cons:* Risk of breaking existing queries, seed scripts, or UI forms.
- **Recommendation:** **Option 3A**. Use a non-breaking additive migration with automated data backfill.

---

### Decision 4: Cryptographically Secure QR Token Architecture
- **Context:** QR codes currently expose `http://localhost:8000/public/track/{token_id}`, allowing trivial sequential enumeration of tokens ([`QR-002`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l259)).
- **Options:**
  - **Option 4A: Dedicated Opaque UUID Column + HMAC Signature (Recommended)**
    - Add a `token_ref` (UUIDv4 string) column to `tokens`.
    - Encode `/public/track/{token_ref}?sig={hmac_sha256}` in the QR code.
    - Scanner validates HMAC against server secret key before resolving token status.
    - *Pros:* Fully decouples public token reference from database primary keys; impossible to enumerate; tamper-evident.
    - *Cons:* Minor schema addition to `tokens`.
  - **Option 4B: Query-Parameter Signature over Plaintext ID**
    - Keep sequential ID but append signature: `/public/track/47?sig=...`.
    - *Pros:* No schema change.
    - *Cons:* Leaks business metrics (e.g. competitor can observe total token volume by inspecting sequential integers).
- **Recommendation:** **Option 4A**. Implement `token_ref` UUID with HMAC-SHA256 signature.

---

### Decision 5: File Storage Strategy for Receipts, QR Codes & Avatars
- **Context:** QR PNGs and PDF receipts are currently stored in local directories (`qr_codes/`, `receipts/`). Phase 1 and 2 introduce profile photos and verification document uploads.
- **Options:**
  - **Option 5A: Configurable Storage Provider (Local FileSystem + S3 Interface) (Recommended)**
    - Abstract storage operations behind a `StorageProvider` interface.
    - Default to local persistent storage (`uploads/`, `receipts/`, `qr_codes/`) for standard development and Docker volumes.
    - Allow optional S3/GCS backend via environment variables for cloud deployments.
    - *Pros:* Flexible, cost-effective for single-server setups, cloud-ready for scale.
    - *Cons:* Requires building a unified storage interface.
  - **Option 5B: Local FileSystem Exclusively**
    - Store all media in local directories mounted via Docker volumes.
    - *Pros:* Simple.
    - *Cons:* Difficult to scale horizontally across multiple instances.
- **Recommendation:** **Option 5A**. Build the `StorageProvider` abstraction with local filesystem default.

---

### Decision 6: SMS Notification Provider Architecture
- **Context:** The system includes Twilio configuration in [`app/config.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/config.py#L72-L74), but domestic Indian deployments frequently require DLT-registered SMS gateways (e.g., MSG91, Fast2SMS) for compliant transactional messaging.
- **Options:**
  - **Option 6A: Pluggable SMS Adapter Pattern (Recommended)**
    - Implement an `SMSProvider` base class with implementations for Twilio, Generic HTTP Webhook, and Console Mock (for testing).
    - Select active provider via `SMS_PROVIDER=twilio|mock|generic` in `.env`.
    - *Pros:* Allows deployment in international or domestic markets without code modifications.
    - *Cons:* Minor engineering overhead to support multiple providers.
  - **Option 6B: Twilio Only**
    - Restrict SMS integration strictly to Twilio.
    - *Pros:* Minimal code.
    - *Cons:* High cost and potential DLT delivery failures for Indian phone numbers.
- **Recommendation:** **Option 6A**. Implement pluggable SMS adapter with Twilio and Mock backends.

---

### Decision 7: Entrance Kiosk & Public Display Device Provisioning
- **Context:** Requirement [`KSK-004`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l159) and [`DSPM-001`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l339) require authenticating kiosk tablets and TV monitors to prevent unauthorized public tampering.
- **Options:**
  - **Option 7A: 6-Digit Pairing PIN Activation Flow (Recommended)**
    - Device opens an activation screen showing a 6-digit numeric pairing code.
    - Organization Head enters the PIN in their dashboard under "Devices".
    - Server exchanges pairing code for a scoped, long-lived Device JWT with restricted permissions (`public_queue:read` or `kiosk:book`).
    - *Pros:* Frictionless setup on smart TVs and kiosk tablets without typing complex passwords; secure revocation.
    - *Cons:* Requires pairing code state machine.
  - **Option 7B: Static Shared Device Passwords**
    - Organization Head creates a static password for all displays.
    - *Pros:* Simple.
    - *Cons:* Vulnerable to credential leakage on public wall-mounted TV browsers.
- **Recommendation:** **Option 7A**. Implement 6-digit pairing PIN activation workflow.

---

## 3. Phase 2 Reconciliation Findings (October 28, 2026)

### Decision 8: Identity-Document Rejection Reason Policy
- **Context:** Master requirements §5.3 requires the reviewer to "approve, reject, or request more information and **record the decision reason**". The current implementation accepts a rejection with no reason and silently substitutes a default text ("Document does not meet verification requirements.") in [`app/services/identity_service.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/identity_service.py) (`review_identity`, line ~188). The test suite (`tests/api/test_identity_verification.py::test_identity_review_desk_approval_and_rejection`) was adjusted in the Phase 2 reconciliation to accept this behavior while still asserting that **a reason is recorded**.
- **Options:**
  - **Option 8A: Accept default-reason substitution (current behavior)**
    - Reviewer may reject without typing a reason; the audit trail always contains a non-empty reason string.
    - *Pros:* Faster review desk; audit trail never empty. *Cons:* Reason may be generic and less useful to the customer or to auditors.
  - **Option 8B: Require an explicit reason on rejection**
    - Add a Pydantic `model_validator` to `IdentityReviewRequest` (`app/schemas/identity.py`) so `action="reject"` with an empty/missing reason returns 422.
    - *Pros:* Matches the strict §5.3 reading; produces more meaningful rejection reasons. *Cons:* Small API contract change; test must revert to asserting 422.
- **Status:** **Deferred to Phase 7 (Trust/KYC hardening)** with user direction to keep current behavior for now. The associated matrix row (USR-003/USR-005 evidence) and the test comment document the divergence. Do not silently change this behavior.

### Decision 9: Sensitive Access Audit Logging Gap (USR-005 / FORM-008)
- **Context:** Downloading a customer's raw identity document (`/user/identity/{id}/document`, `/org/identities/{id}/document`) and viewing full KYC values are authorized but **not audited**; USR-005 requires recording every authorized access to full values/documents.
- **Recommendation:** Before Phase 8 security hardening, route document downloads and any future full-value reveal endpoints through the existing audit-log service (`models/audit_log.py`) with actor, target, timestamp, and outcome.
- **Status:** Recorded as an unresolved gap; no code change made during Phase 2 reconciliation.

---

## 4. Phase 3 Design Decisions (October 28, 2026)

Decisions below were verified against MASTER_REQUIREMENTS §6.2, §7.1-7.5, §8.4, §9.2, §20, §24.2, §25 (items 8, 9) and matrix rows SCH-001..009 before being recorded.

### Decision 10: Booking Capacity Model — RESOLVED
- **Decision:** Hybrid model. Counters/services are configured as `queue_mode = day_queue | slots` (a day-queue token is one all-day "slot" spanning operating hours; an appointment-slot mode exposes configurable 15/30/60/custom-minute slots).
- **Rationale:** MASTER_REQUIREMENTS §25 item 8 explicitly leaves this open ("time slots, daily token quota, live walk-in queue, appointments, or a combination") and SCH-005 permits "day-queue or configurable 15/30/60/custom-minute slots". Supporting both satisfies the requirement; defaulting to `day_queue` preserves all Phase 1-2 booking behavior (backward-compatibility rule) because existing counters carry no schedule data.
- **Constraint:** Daily issuance limits (Decision 13), per-slot capacity, counter daily limits, service-level limits, and source-based allocation (SCH-004; CTR-003) are enforced as **independent, layered checks** — none may contradict §7.4's requirement that all channels pass the same authoritative capacity checks.

### Decision 11: Schedule-Change Impact Workflow — RESOLVED (with Phase 5 dependency)
- **Decision:** When a schedule or exception edit affects confirmed tokens, the system must (1) detect and list the affected bookings, (2) require an explicit organization decision to **honor, reschedule, or cancel** each affected booking, (3) record the decision (actor, time, reason, outcome) and notify affected users.
- **Deferral:** Refund *execution* is deferred to Phase 5 payment integration, but the impact record preserves payment references and amount data so Phase 5 can process refunds later — consistent with MASTER_REQUIREMENTS §8.4 ("Payment/refund implications must be handled explicitly", "Changes must be auditable") and §12.5 ("Do not delete the original payment history").
- **Scope note:** SCH-007 is fully covered by (1)-(3); only the refund leg awaits Phase 5.

### Decision 12: Time Zones — RESOLVED
- **Decision:** All timestamps stored in UTC (existing convention: `token_service.py:32,171`). Every organization and every branch carries an IANA timezone identifier (e.g., `Asia/Kolkata`); branch timezone overrides the organization timezone when set; rendering uses the most specific applicable timezone.
- **Rationale:** SCH-009 mandates "an IANA timezone per organization/**branch**" — branch-level override is therefore **required**, not optional. Default value `Asia/Kolkata` (project's primary deployment market; recorded as the default, configurable per tenant). MASTER_REQUIREMENTS §13.3 (timezone for reconciliation business-day boundaries) is a Phase 5 consumer of the same fields.

### Decision 13: Daily-Limit Semantics for Cancelled Tokens — RESOLVED (Option 13A, confirmed October 28, 2026)
- **Context:** Current behavior counts **every token created today** at a counter against `counter.daily_limit`, regardless of status; cancelling a token does **not** release the daily quota (`app/services/token_service.py:170-178` — `count(Token WHERE counter_id, created_at >= today)`; `perform_token_action` cancel releases nothing).
- **Requirements check:** MASTER_REQUIREMENTS is silent on whether cancelled tokens release daily capacity. §9.2 addresses sequence-number reset/preservation, not capacity; §8.4 requires cancellation to update the authoritative booking but says nothing about quota release. The question is therefore **not settled** and is recorded rather than guessed.
- **Decision:** **Option 13A confirmed.** Daily issuance quota is consumed at token creation and is **never released** by cancellation — simple, abuse-resistant (prevents cancel/rebook churn to bypass limits).
- **Invariant:** the daily **issuance** limit stays distinct from **concurrent active-booking capacity** (slot capacity, active queue size). Note: this applies to the *counter daily limit* (`counter.daily_limit`); a future `capacity_rules` rule could still define release semantics independently if a requirement ever demands it.

### Decision 14: Payment-Reservation Expiry Integration — RESOLVED (design contract only)
- **Decision:** Phase 3 introduces the *reservation hook points* only: slot reservations may be created with status `held` and an `expires_at`; an expired hold releases capacity without ever inventing payment logic. No payment/refund implementation in Phase 3.
- **Phase 5 integration contract:** SCH-006's "payment reservations expire and release capacity" will be satisfied by (1) Razorpay order creation placing a *held* reservation, (2) webhook/signature confirmation promoting it to *confirmed*, (3) a sweeper (background task or lazy check on read) releasing expired holds. Holds that expire must be idempotent to release. This matches MASTER_REQUIREMENTS §12.3 (server-side verification; "Define what happens when payment succeeds but booking confirmation or network response fails").
- **Phase 5 implementation (Oct 31 2026):** the contract is live. Razorpay order creation (`payment_service.create_payment_order`) attaches a `PENDING` payment to the slot-reserved token; confirmation paths — client verification (`payment_service.verify_payment`) and the webhook (`webhook_service.process_webhook_event`) — both mark the payment `PAID` exactly once (PAY-005 single-confirmation guard: an already-paid payment returns early with no duplicate ledger charge) and post the gateway→revenue double entry. Expired/abandoned holds are reclaimed by the sweeper `payment_service.release_expired_payment_holds` (payment_service.py:141): it returns every `time_slots` row with `held_count > 0`, counts unconfirmed (`PENDING`) payments joined through `tokens.slot_id`, and releases only the *excess* holds (`held_count − pending_count`) back to inventory — idempotent by construction, since re-running it on a steady state computes zero excess. Tested by `tests/unit/test_ledger_and_finance.py::test_release_expired_payment_holds_sweeper` (12-test Phase 5 module, full suite 403/403 green). Judgment note: the sweeper keys on active `PENDING` payments rather than per-hold `expires_at` timestamps — reservations themselves are guarded by the atomic slot reservation and the webhook single-confirmation window, so no overbooking path exists; a per-hold TTL field remains a possible future hardening, deliberately not invented now.

### Decision 15: Phase 3 Discovery Scope — RESOLVED
- **Decision:** Phase 3 search enhancements are limited to the roadmap's backend bullet: service filtering, branch/pincode filtering, and available slot-date filtering. All other SRCH rows (sorting modes SRCH-003, query UX SRCH-004/007, result cards SRCH-006, typo tolerance SRCH-008) are deferred to the Phase 9 customer-experience phase.

### Data-Model Design Notes for Phase 3 (not decisions — constraints)
- **Schedule scoping:** `weekly_schedules` and `schedule_exceptions` use `(scope_level, scope_id, organization_id)` rather than three nullable FKs, so uniqueness constraints are enforceable (NULL FK columns are treated as distinct by both MySQL and SQLite, which would break a uniqueness guarantee on nullable columns). `scope_level ∈ {org, branch, counter, service}`; precedence service > counter > branch > org (SCH-002). All rows carry `organization_id` directly for tenant isolation (AUTHZ-001).
- **Uniqueness:** `weekly_schedules(scope_level, scope_id, day_of_week)`; `schedule_exceptions(scope_level, scope_id, exception_date)`; `time_slots(counter_id, slot_date, start_time)`; `capacity_rules(scope_level, scope_id, source)`.
- **Atomicity:** slot reservation uses a guarded atomic `UPDATE … SET booked_count = booked_count + 1 WHERE id = :id AND booked_count < capacity` with rowcount verification — portable across MySQL and SQLite (the test backend does not support `SELECT … FOR UPDATE`; the roadmap's optimistic-versioning alternative is honored via this guarded update).
- **Backward compatibility:** counters/services with **no schedule data** continue to book as day-queue exactly as in Phases 1-2 (no NOT_OPEN regression); publishing user-visible availability (SCH-001) begins at first schedule configuration.
- **Token linkage:** `tokens.slot_id` is a nullable FK (`SET NULL` on slot deletion); day-queue tokens have `slot_id = NULL`, so existing flows, history, and Phase 2 snapshots are untouched.
- **Slot-time representability (post-review constraint, Oct 28 2026):** slot boundaries are stored as the pair `(slot_date, UTC time-of-day)`; a local wall-clock time whose UTC conversion lands on a different calendar date cannot be represented by that pair. Slot generation therefore **fails loudly (HTTP 400)** when operating hours cross a UTC date boundary (e.g., hours starting before 05:30 local at `Asia/Kolkata`, or after ~19:00 local in a UTC−5 zone) instead of silently storing wrong instants. Operationally this requires operating hours to map to the same UTC calendar date — true for all realistic clinic hours in the default timezone.
- **Calendar past-date guard (post-review fix, Oct 28 2026):** the published-availability calendar resolves past dates but never generates slots for them, matching `ensure_slots_for_date`; legacy tenants (no published schedule anywhere) with an active special-hours or reduced-capacity exception have that exception honored rather than silently discarded.
- **Same-day cutoff (Oct 29 2026, SCH-005):** slot bookings are accepted up to the slot's start instant and never after. `token_service._validate_and_reserve_slot` decodes the stored `(slot_date, UTC time-of-day)` pair into the exact UTC instant and rejects `start <= now` ("This time slot has already started and can no longer be booked.") — an instant-based check, not a naive wall-clock comparison. The public slot-date calendar (`get_public_slot_dates`) excludes started slots from today's advertised remaining capacity (unstarted-only aggregate, plus an unfiltered aggregate so a fully-elapsed day shows FULL/0 rather than legacy "no inventory"); day-level SCH-008 states are unchanged. Day-queue legacy issuance is untouched. Both services expose an injectable `_utcnow` clock seam used by the frozen-clock cutoff tests. The cancel/reschedule cutoff is deliberately NOT implemented: no customer self-service cancel/reschedule channel exists yet (Phase 6), and OPEN Decision 16 must settle cancellation/capacity-release semantics before cancellation behavior is touched.
- **Slot-ladder rendering (Oct 29 2026, SCH-009):** the stored `(slot_date, UTC time-of-day)` pair is rendered back to the business-local wall clock through the tenant zone (branch override wins, Decision 12) by a documented inverse of the generation conversion (`utc_pair_to_local_time`); the absolute UTC instants ride along for machine consumers. The public slot ladder (`GET /public/organizations/{org_id}/slots`) is a pure read/render view with the same gates as the booking engine: started slots are never listed (same-day cutoff consistency), filled-but-unstarted slots stay visible with remaining 0 and `is_bookable = False`, non-bookable day states gate to an empty list, legacy tenants surface `is_legacy_day_queue`, and the date horizon matches booking (`today ≤ date ≤ today + 60` tenant-tz). No token writes, no capacity semantics beyond `cap − booked − held`, no Decision 16/17 interaction.
- **Capacity-rule engine (Oct 29 2026, SCH-004):** `token_service._validate_capacity_rules` applies the rules as independent, layered checks inside the booking transaction BEFORE the slot reservation (per Decision 13's reconciliation note; §7.4 same-authoritative-checks across channels). Counting semantics, deliberately fixed to avoid inventing policies: (1) the daily bucket is the ISSUANCE day — the UTC calendar day of `created_at`, identical to the pre-existing `counter.daily_limit` bucket (Decision 13A: quota consumed at token creation, never released; §13.3 tenant-business-day reconciliation remains a Phase 5 consumer per Decision 12); (2) booking source is SERVER-SIDE per channel (`kiosk` from the kiosk route, `online` from `/user/tokens/book`, `walk_in` default for direct service calls — never client input; new `tokens.booking_source` column, migration `0008`, legacy rows backfilled `walk_in`); (3) branch scope counts tokens at all counters of the branch, counter/service/org scopes as keyed; (4) WALK_IN_QUOTA is definitionally walk-in-only (counts and rejects walk-in issuances; other channels neither consume nor confront it); (5) PER_USER applies to authenticated users — guest bookings carry no user identity and cannot be per-user-limited; (6) PRIORITY_RESERVE (CTR-003) withholds `max_count` places from ordinary sources inside the same-scope aggregate (`all`-source) DAILY_TOKEN cap; the reserve's own source consumes the withheld headroom; a reserve with no aggregate rule at its scope is inert (nothing to modify). Reserves of OTHER sources are fetched even when booking through a different source (foreign reserves ARE withheld from the requester). Concurrency posture: slot capacity keeps the atomic guarded UPDATE; the count-based daily rules are layered checks sharing the pre-existing `_next_sequence`-style bounded race under genuinely parallel writers (same posture already documented for `counter.daily_limit` — no stronger atomicity claimed for count-based buckets).
- **Atomic slot reservation (Task 3, Oct 28 2026):** `token_service._validate_and_reserve_slot` consumes one place with a guarded conditional `UPDATE time_slots SET booked_count = booked_count + 1 WHERE id = :id AND booked_count + held_count < capacity` verified by rowcount, executed in the SAME transaction as the token insert — `book_token` commits once, so a token-creation failure (QR failure, IntegrityError at flush/commit) rolls back reservation + token together, and no code path increments occupancy twice. The guard re-evaluates against the current committed row inside the write, so concurrent requests competing for the final place serialize safely without `SELECT … FOR UPDATE` (portable to MySQL InnoDB and SQLite; MySQL locking is NOT yet exercised — deployment gate).
- **Slot-booking eligibility (Task 3):** a `slot_id` booking validates the slot exists for the tenant (counter + organization ownership), belongs to the selected counter (or routes to the slot's own counter when no counter was given), is active, lies within `[today, today + 60 days]` in the tenant timezone, and its business date resolves to an open availability state — CLOSED/HOLIDAY/NOT_OPEN/SERVICE_UNAVAILABLE/FULL reject with the user-facing reason (SCH-003/008 wired into the booking engine). Slot-mode counters require an explicit `slot_id`; auto-routing without a slot only considers day-queue counters. Day-queue behavior (including `slot_id = NULL` legacy tokens) is untouched; Decision 13A daily-quota semantics unchanged. `IntegrityError` at flush/commit is converted to a generic retryable message (no DB internals leaked).
- **Discovery implementation (Task 4, Decision 15 scope):** `search_organizations` gains service/PIN/slot-date filters and distance search. Distance uses nullable `geo_lat`/`geo_lng` on `branches` (migration `0006`, per the architecture proposal's branch data model — organizations are NOT geocoded directly); distance = Haversine to the organization's nearest geocoded active branch, computed in Python (candidates capped at 1000) because SQLite test compatibility outweighs SQL-dialect Haversine at discovery scale; SQL-level pagination is bypassed only in distance mode, and `radius_km` excludes ungeocoded organizations while coordinate-only queries return them last with `distance_km = None` (an honest "no location data" signal, not a silent drop). Public slot-date discovery (`get_public_slot_dates`) lazily generates slots for the horizon with the same idempotent generator the booking flow uses, then aggregates remaining capacity per date across ACTIVE slot-mode counters; day states (closures/holidays/NOT_OPEN) come from the same SCH-002/008 resolution as booking, so the discovery calendar can never advertise a date the booking engine would reject. Inactive services are rejected at booking on every channel (audit-defect #1 closure): `book_token` validates `service.is_active` directly and passes the service into day resolution so SERVICE_UNAVAILABLE applies to slot bookings.
- **Known pre-existing limitation (documented, not fixed in Task 3):** `_next_sequence` (`MAX(sequence)+1`) has a read-race under concurrent bookings at the same counter — no unique constraint backs `(counter_id, sequence)`/`token_number`, so simultaneous issuances can produce duplicate sequence numbers. Unchanged from Phases 1-2; a dedicated fix (constraint + retry) belongs to the queue-engine work.
- **Backfill risk:** all new tables are additive with no data backfill; only `organizations.timezone` (server default `Asia/Kolkata`) and `tokens.slot_id` (nullable) alter existing tables — low migration risk. Alembic chain: `0005` revises `0004`.

### Decision 16: Cancellation & Capacity Release - OPEN (reconciled with Decision 13A)
- **Question:** when a slot-based token is cancelled (or its booking actioned), should the slot's `booked_count` be decremented so the place returns to inventory, and should Decision 13A's "cancelled tokens still consume daily quota" semantics apply per-slot or per-counter?
- **Status:** OPEN — deliberately NOT implemented. Task 3's read-only audit surfaced this gap (cancelled tokens never release slot occupancy), and the schedule-management task was explicitly gated on this decision: no cancellation or capacity-release behavior was invented or modified while Decision 16 remains undecided. Current behavior (unchanged from Task 3): a cancelled slot token's place stays consumed in `time_slots.booked_count`.
- **Reconciliation with Decision 13A (§8.4):** Decision 13A fixed the *day-queue* daily-limit semantics (cancelled tokens still count against the counter's `daily_limit` to prevent repeat rebooking abuse). Decision 16 concerns *slot* occupancy, which Decision 13A does not address. Any resolution must state explicitly whether daily quota and slot occupancy release together or independently for slot-mode counters.
- **Constraints for whoever resolves this:** release must be atomic with the cancellation commit (same pattern as Task 3's guarded reservation: conditional `UPDATE … WHERE booked_count > 0` with rowcount verification, no decrement below zero, CHECK `booked_count >= 0` already protects); payment-funded cancellations interact with Decision 14 (hold/expiry sweeper, Phase 5); the resolution must be implemented consistently across every cancel path (user self-cancel, reschedule replacement, admin action, no-show/expiry) so occupancy never double-decrements or leaks.
- **Impact if left open:** FULL days/slots never recover places after cancellations — capacity monotonically decreases until midnight of the slot date rolls past; harmless for short horizons but visibly wrong for 60-day-advance booking.

### Decision 17: Schedule-Edit Impact on Generated Slots - RESOLVED (scope rule, Oct 29 2026)
- **Rule:** schedule/exception/capacity CRUD never modifies already-generated `time_slots` rows or existing bookings. Impact on confirmed tokens is handled exclusively by the Decision 11 / SCH-007 impact workflow (honor/reschedule/cancel/refund), which remains unimplemented (its authorization was the explicit prerequisite not met for this task — see the "Unmet requirement" note in the Task 5 report).
- **Rationale:** a CRUD write silently deleting or shrinking `time_slots` would strand `tokens.slot_id` references and bookable places without the controlled workflow §8.4 and SCH-007 require. Existing slots only change occupancy through the booking engine; stale slots for reconfigured future days are naturally superseded because `ensure_slots_for_date` regenerates lazily only while the horizon guard allows, and slot consumption never recreates rows for configured windows (generation is idempotent on `(counter_id, slot_date, start_time)` — an earlier reviewer must re-run the SCH-005 regeneration tests before changing this).
- **Consequences:** (1) after CRUD edits, previously generated slots for affected future dates keep their old boundaries until the impact workflow runs; (2) the DaySafeguards tests assert bookings and slots are untouched after hour edits/deletes; (3) if future work wants eager regeneration on CRUD writes, it MUST go through the Decision 11 workflow first.
- **Status:** RESOLVED as a scope rule for the schedule APIs; the Decision 11 impact workflow itself remains OPEN as a Phase 3 remaining item (SCH-007 stays Missing).

### Decision 18: FIN-003 Risk Thresholds & Waiver/Price-Change Rule Basis — PROVISIONAL (Oct 31, 2026)
- **Problem:** MASTER_REQUIREMENTS defines the FIN-003 rule *categories* (unusual volume, duplicate references, after-hours, waivers, price changes, payment-token mismatch) but states **no numeric thresholds** for them — and Phase 5 closure rules forbid inventing business policy.
- **Provisional basis applied (consistent with the pre-existing ₹10,000 unusual-amount / 5-refunds / ₹200 cash thresholds):**
  - *Unusual volume:* ≥ **100** payment records per organization within 24 hours (`VOLUME_THRESHOLD_24H` in `financial_risk_service.py`), single severity tier (MEDIUM).
  - *Duplicate references:* no threshold — any organization-scoped reuse of `payments.razorpay_order_id` or `tokens.client_reference` across different records raises one flag (HIGH).
  - *After-hours:* no threshold — any transaction whose timestamp falls outside the organization's **published** operating hours in its own IANA timezone (schedule-service resolution, exception-aware; overnight `open>close` handled defensively). Skipped entirely for tenants with no published schedule (legacy day-queue) so nothing is judged against an absent policy.
  - *Waivers:* **presence-based** — every payment with `method=waived` is surfaced (LOW) for proportional review. No monetary waiver threshold is invented; the update-path's existing reason / supervised-approval policy (`waiver_requires_approval`, `waiver_approval_threshold` org settings) is unchanged and remains the only *blocking* waiver control.
  - *Price changes:* **presence-based** — any catalogue price edit (base service or sub-service) while active tokens still snapshot a different price raises one flag (MEDIUM); no change-frequency or amount limit is invented.
  - *Payment-token mismatch:* no threshold — any invariant violation (organization mismatch, confirmation on a cancelled/completed/skipped token, more than one successful payment per token, amount vs. booked snapshot) raises one flag; severity escalates with the worst leg.
- **Open question for the business owner:** confirm or replace the provisional volume threshold (100/24h) and decide whether waiver/price-change flags should stay presence-based or gain monetary thresholds. Flags are review-only (FIN-004: proportional, never punitive), so an over-broad threshold costs attention, not revenue — conservative defaults were chosen deliberately.
- **Status:** PROVISIONAL. All rules implemented, wired and tested (tests/unit/test_finance_policy_and_risk_rules.py, 25 closure tests); only the threshold VALUES above await business confirmation.

### Decision 11 Workflow Implementation Notes (Oct 29, 2026 — SCH-007 implemented)
- **Implementation:** the workflow exists as a separate API (6 endpoints under `/org/schedules/changes`, see `docs/api.md` §Schedule-Change Impact Workflow) backed by `app/services/schedule_change_service.py` and the `schedule_change_previews` table (migration `0007`). Preview validation reuses the SAME commit-free validation cores the schedule CRUD wrappers use (`_new_weekly_values`, `_prospective_*_values`); impact is computed through the ONE resolution engine (`schedule_service`) extended with additive shadow-overlay parameters (`schedule_overlay`, `exception_overlay`, `capacity_base`) so previews rerun exactly the production resolution — no second engine, no writes.
- **Safety rails:** (1) `impact_digest` (sha256 over the tenant's configuration rows + the affected counters' slots/occupancy/token states within the horizon) is recomputed at confirm — mismatch → preview auto-marked `stale`; (2) confirmation claims the row via a conditional `UPDATE … WHERE status='pending'` (single-apply, race-safe); (3) missing per-booking decisions block confirm (`409`); (4) preview/resolve/confirm/reject are audited (`schedule.change.*`); TTL 120 minutes → `expired`.
- **Decision 16 boundary preserved:** `confirm` NEVER releases slot capacity. Cancel decisions change only token state (history + queue log, reusing the `token_service` action primitives inside the confirm transaction); `booked_count` stays consumed; `reschedule` decisions are recorded with a `reschedule_blocked` outcome citing this decision — executing them requires an approved capacity-release policy (still OPEN). Decision 13A daily quota is untouched.
- **Refund leg:** still Phase 5 (per Decision 11); `time_slots.held_count` still has no writer outside the booking guard (Decision 14/Phase 5).
- **MySQL deployment gates (unchanged):** the explicit-preview flow was validated on SQLite; MySQL InnoDB locking and migration `0007` execution on MySQL remain deployment-gate items.

### Shift Workflow Implementation Notes (Oct 30, 2026 — SHIFT-001/002 implemented)
Workflow/engineering notes for the daily counter shift state machine, recorded because the roadmap enum alone leaves four cases unspecified. These are **implementation decisions, not tenant policy**; revisit if the head panel later needs different behavior.
1. **No `REJECTED` status.** The requirements deliverable list (roadmap DB bullet) has no rejected value, and SHIFT-002 only requires approval/rejection *with reason*. A rejection therefore keeps the row `OPEN_REQUESTED` with `decided_reason` recorded — the operator corrects the submission and resubmits (same row updated in place), which is exactly the "request correction" vocabulary SHIFT-006 uses. Re-deciding a decided row is blocked (`409`).
2. **`APPROVED` ≠ `ACTIVE` (two-step clock-in).** Approval grants the desk permission; the requesting employee activates their own approved shift (`POST /employee/shifts/{id}/activate`) so a shift is never ACTIVE without a prior approval AND without the operator physically clocking in. Activation is restricted to the tenant's current business day and, when the head set one, inside the approved period (tenant-local times). Both statuses in the roadmap enum are thus reachable and meaningful.
3. **Delegated manager = explicit RBAC permission.** "Organization head or explicitly delegated manager" is implemented with the existing RBAC mechanics: a new catalog permission `shifts:approve` (seeded by `seed_rbac_defaults`); the organization principal always passes, an employee passes only when assigned a role carrying `shifts:approve` (assignment via the existing `roles:assign` flow). The default `branch_manager` role does NOT include it — delegation must be explicit. `decided_by_type/id` records who decided.
4. **Duplicate-request rule.** One non-terminal shift per (employee, counter, tenant day): resubmitting while `OPEN_REQUESTED` updates that row; requesting again when a non-terminal shift (`APPROVED`/`ACTIVE`/`PAUSED`/`CLOSE_REQUESTED`/`RECONCILING`) exists is rejected. Terminal `CLOSED` rows never block a new daily request. No DB unique constraint backs this — it is a service-layer rule under normal (non-concurrent) request pacing; closing-workflow tasks may revisit.
5. **SHIFT-003 boundary.** Only the gate primitive `shift_service.require_active_shift` (raising `PermissionError`) and `get_active_shift` exist now; wiring them into token issuance/cash/queue endpoints is SHIFT-003, a separate Phase 4 task — not wired here on purpose.
6. **Deployment gate:** migration `0009` is SQLite-validated only (test `create_all` + migration chain compiles); MySQL execution remains a deployment-gate item like `0005`–`0008`.

---

## 5. Next Steps & Authorization Request

Upon user review of the audit findings, architecture proposal, and open decisions:
1. Confirm preferred options for Decisions 1 through 7.
2. Authorize initiation of **Phase 0 (Baseline Stabilization & Test Suite Green)**.
3. *Update (Oct 28, 2026):* Phases 0–1 committed; Phase 2 (Catalogue) complete in working tree, pending commit. Decisions 8–9 recorded above.
4. *Update (Oct 28, 2026, post-commit `d768faa`):* Phase 2 committed. Phase 3 design decisions 10-15 recorded above — all now **resolved**; Decision 13 confirmed as Option 13A by the user (Oct 28, 2026). No remaining design blockers for Phase 3; awaiting user authorization to begin implementation.
