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

## 4. Next Steps & Authorization Request

Upon user review of the audit findings, architecture proposal, and open decisions:
1. Confirm preferred options for Decisions 1 through 7.
2. Authorize initiation of **Phase 0 (Baseline Stabilization & Test Suite Green)**.
3. *Update (Oct 28, 2026):* Phases 0–1 committed; Phase 2 (Catalogue) complete in working tree, pending commit. Decisions 8–9 recorded above.
