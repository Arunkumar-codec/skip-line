# SkipLine: The Smart Queue System — Architecture Proposal

> **Document Version:** 1.0.0  
> **Date:** October 12, 2026  
> **Status:** Draft Proposal for Architectural Alignment  
> **Reference Baseline:** [`docs/quirments.docx`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/quirments.docx), [`docs/PROJECT_AUDIT.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/PROJECT_AUDIT.md), [`docs/FEATURE_REQUIREMENTS_MATRIX.md`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md)

---

## 1. Architectural Vision & Principles

SkipLine is a multi-tenant digital queue management and virtual token platform. The primary architectural objective is to transition from the current 12-table monolithic baseline to an enterprise-grade, domain-driven, event-synchronized platform without discarding working code or destabilizing existing user flows.

### Core Architectural Principles
1. **Preserve Working Baseline ([`ARC-001`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l35)):** Do not discard or rewrite working FastAPI routers, SQLAlchemy async patterns, or Jinja2 layouts merely for cosmetic reasons. Evolve through schema migrations and service-level refactoring.
2. **Strict Multi-Tenant Isolation ([`AUTHZ-001`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l47), [`AUTHZ-002`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l48)):** Every tenant-owned entity links unequivocally to an `organization_id` (and where applicable, `branch_id`). Tenant context is derived strictly from authenticated session claims; client-provided tenant IDs are never trusted.
3. **Decoupled Principals & Fine-Grained RBAC ([`ARC-003`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l37), [`AUTHZ-005`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l51)):** Separate overloaded organization credentials into explicit Organization Heads, Delegated Managers, and Staff Employees. Authorize actions through named permissions rather than hardcoded role strings.
4. **Immutable Financial Ledger ([`ARC-004`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l38), [`LED-001`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l223)):** Money movements are append-only. No payment records are mutated or deleted. Cash collections reconcile per employee shift with mandatory supervisor sign-off.
5. **Secure Real-Time State Distribution ([`QUE-005`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l203), [`QR-002`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l259)):** Re-establish the broken WebSocket broadcast pipeline. QR tokens encode cryptographic signatures or opaque references, completely eliminating token enumeration vulnerabilities.
6. **No-Dummy Interface Standard ([`UIQ-001`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l409)):** Every UI button, toggle, and navigation element connects to an authoritative backend API with complete loading, empty, success, and error states.

---

## 2. Target High-Level System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                             CLIENT ACCESS CHANNELS                               │
├─────────────────┬──────────────────┬───────────────────┬─────────────────────────┤
│ Customer Mobile │ Public TV Screen │ Self-Serve Kiosk  │ Employee Counter Station│
│ (Web/PWA/QR)    │ (Display Room)   │ (Guest Booking)   │ (Operator Desktop)      │
└────────┬────────┴────────┬─────────┴─────────┬─────────┴────────────┬────────────┘
         │                 │                   │                      │
         ▼                 ▼                   ▼                      ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                         NGINX REVERSE PROXY & GATEWAY                            │
│  - SSL/TLS Termination      - WebSocket Connection Upgrade   - Rate Limiting     │
│  - Static Asset Caching     - IP Whitelisting (Admin)        - Host Verification │
└────────────────────────────────────────┬─────────────────────────────────────────┘
                                         │
                                         ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                             FASTAPI APPLICATION LAYER                            │
│                                                                                  │
│ ┌──────────────────────────────────────────────────────────────────────────────┐ │
│ │                         MIDDLEWARE & SECURITY FILTER                         │ │
│ │  - Logging & Profiling        - Session Middleware     - Host Validation     │ │
│ │  - CSRF Protection (Double-Cookie) - Sliding Window IP/Device Rate Limiter   │ │
│ └──────────────────────────────────────┬───────────────────────────────────────┘ │
│                                        │                                         │
│ ┌──────────────────────────────────────┴───────────────────────────────────────┐ │
│ │                         AUTHENTICATION & RBAC ENGINE                         │ │
│ │  - Principal Resolution (Admin, Org Head, Employee, User, Device)            │ │
│ │  - Fine-Grained Permission Enforcement (require_permission)                  │ │
│ │  - Tenant & Branch Boundary Isolation (require_tenant_context)               │ │
│ └──────────────────────────────────────┬───────────────────────────────────────┘ │
│                                        │                                         │
│ ┌──────────────────────────────────────┴───────────────────────────────────────┐ │
│ │                      ROUTERS & INTERFACE CONTROLLERS                         │ │
│ │  ├── Admin REST & Views       ├── Org Head REST & Views ├── Employee Portal  │ │
│ │  ├── Customer Portal & Views  ├── Public Kiosk & Track  ├── QR & Receipts    │ │
│ │  ├── Dynamic Form Builder     ├── Shift & Cash Desk     ├── Support & Cases  │ │
│ │  └── WebSocket Endpoints      └── Payment & Webhooks    └── Device Manager   │ │
│ └──────────────────────────────────────┬───────────────────────────────────────┘ │
│                                        │                                         │
│ ┌──────────────────────────────────────┴───────────────────────────────────────┐ │
│ │                           DOMAIN SERVICE LAYER                               │ │
│ │  ├── Queue & Token Engine     ├── Appointment Scheduling├── Payment Ledger   │ │
│ │  ├── Shift & Cash Reconcile   ├── Dynamic Form Engine   ├── Support Engine   │ │
│ │  ├── QR Cryptography & Scan   ├── Notification Outbox   ├── KYC & Audit      │ │
│ └──────────────────────────────────────┬───────────────────────────────────────┘ │
└────────────────────────────────────────┼─────────────────────────────────────────┘
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 ▼                                               ▼
┌───────────────────────────────────┐           ┌──────────────────────────────────┐
│       PRIMARY DATA STORE          │           │       REAL-TIME BUS & CACHE      │
│  MySQL 8.3 (InnoDB, UTF8mb4)      │           │  Redis 7 (Pub/Sub & Cache)       │
│  - 35+ Normalized Domain Entities │           │  - Cross-Worker WS Broadcasting  │
│  - Foreign Keys & ACID Constraints│           │  - Sliding Window Rate Limits    │
│  - Decimal Financial Precision    │           │  - Active Shift Cache            │
│  - Comprehensive Composite Indexes│           │  - Kiosk Session Transient Memory│
└───────────────────────────────────┘           └──────────────────────────────────┘
```

---

## 3. Domain Model Blueprint (35+ Core Entities Across 11 Domains)

To satisfy Section 18 of the SRS ([`docs/quirments.docx`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/quirments.docx)), the current 12-table model will be expanded across 11 discrete domains:

### Domain 1: Identity & Profiles
- `users`: Core end-user account (id, email, mobile, password_hash, is_active, created_at, updated_at).
- `user_identities`: Submitted government ID proofs (Aadhaar, PAN, Passport) for verified services.
- `profile_images`: Secure storage paths and audit metadata for avatar uploads.
- `user_verification_events`: Audit log of identity verifications and administrative rejections.

### Domain 2: Tenancy & Multi-Branch Hierarchy
- `organizations`: Tenant business profile, registration number, category, primary address, status (`DRAFT`, `SUBMITTED`, `UNDER_REVIEW`, `APPROVED`, `SUSPENDED`, `REJECTED`).
- `branches`: Physical locations under an organization (id, organization_id, name, code, address, city, state, pincode, geo_lat, geo_lng, is_active).
- `organization_representatives`: Designated points of contact, business owners, and legal signers.
- `organization_documents`: Multi-category verification documents (tax certificate, trade license, clinical establishment proof) with review status and expiry.
- `organization_appeals`: Formal dispute and appeal filings following suspension or rejection.

### Domain 3: Authorization & Role-Based Access Control (RBAC)
- `employees`: Dedicated staff accounts (id, organization_id, branch_id, name, email, employee_code, password_hash, is_active, created_at).
- `roles`: Role definitions per tenant or system-wide (id, organization_id, name, code, description).
- `permissions`: Fine-grained permission dictionary (`counter.open.request`, `counter.open.approve`, `token.call`, `cash.collect`, `shift.reconcile`, `refund.request`, `support.reply`).
- `role_permissions`: Many-to-many junction binding roles to explicit permissions.
- `employee_role_assignments`: Assignment of roles to employees scoped to branches or counters.
- `sessions`: Server-tracked principal sessions enabling instant token revocation.

### Domain 4: Service Catalogue & Multi-Service Routing
- `services`: Master services offered (id, organization_id, branch_id, name, base_price, estimated_minutes, is_active).
- `sub_services`: Specialized sub-categories (e.g., General OPD -> Pediatric, Dental, Orthopedic) with differential pricing.
- `counter_services`: **Many-to-many junction** linking counters to multiple eligible services ([`ARC-002`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l36), [`CTR-001`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l135)).
- `service_prices`: Historical price revision records preventing historical ledger corruption.

### Domain 5: Dynamic Custom Form Builder
- `custom_forms`: Form definitions bound to services (id, organization_id, service_id, title, version, is_active).
- `form_fields`: Individual form inputs (field_type: `text`, `select`, `date`, `file`, `checkbox`; label, validation_rules, order_index).
- `form_options`: Select/radio option dictionaries.
- `form_conditions`: Conditional visibility rules (e.g., show field Y if field X == "Yes").
- `token_form_snapshots`: **Immutable JSON snapshot** of the form structure at time of booking ([`DB-003`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l363)).
- `token_form_answers`: User responses captured during booking, encrypted at rest where sensitive.

### Domain 6: Scheduling, Availability & Atomic Capacity
- `weekly_schedules`: Operating hours per branch/service per day of week (open_time, close_time, slot_interval_minutes).
- `schedule_exceptions`: Overrides for public holidays, emergency closures, or special extended hours.
- `time_slots`: Atomic booking slots (branch_id, service_id, slot_date, start_time, end_time, max_capacity, booked_capacity).
- `capacity_rules`: Daily maximums, emergency buffer reservations, and walk-in quotas.

### Domain 7: Queue Engine, Shifts & Counter Operations
- `counters`: Physical or virtual service points (id, branch_id, counter_name, counter_number, token_prefix, status).
- `counter_shifts`: Daily counter operational sessions (id, counter_id, employee_id, shift_date, status: `CLOSED`, `OPEN_REQUESTED`, `APPROVED`, `ACTIVE`, `PAUSED`, `CLOSE_REQUESTED`, `RECONCILING`).
- `tokens`: Virtual queue tokens (id, token_number, sequence, status: `RESERVED`, `PAYMENT_PENDING`, `CONFIRMED`, `WAITING`, `CALLED`, `RECALLED`, `ON_HOLD`, `IN_PROGRESS`, `COMPLETED`, `SKIPPED`, `CANCELLED`, `EXPIRED`, `NO_SHOW`).
- `token_history`: Immutable status transition audit log.
- `queue_events`: Fine-grained operational metrics (call duration, wait duration, hold duration).

### Domain 8: Finance, Ledger & Cash Reconciliation
- `payments`: High-level payment order records (id, token_id, amount, currency, method, status).
- `payment_items`: Line items capturing base service price, sub-service add-ons, and taxes.
- `ledger_entries`: **Append-only double-entry financial journal** (id, transaction_ref, entry_type: `DEBIT`, `CREDIT`, account: `USER`, `ORG_ESCROW`, `GATEWAY_HOLD`, `PLATFORM_FEE`, amount, created_at).
- `cash_sessions`: Cash drawer collection records per shift (starting_float, collected_cash, counted_cash, discrepancy, status: `UNRECONCILED`, `MATCHED`, `SHORTAGE`, `EXCESS`, `APPROVED`).
- `refunds`: Refund requests with reason, gateway refund ID, admin approval, and ledger entries.
- `settlements` & `payouts`: Periodic payout calculations and bank transfer records for organizations.

### Domain 9: Trust, Verified Feedback & Customer Support
- `feedback`: Customer ratings (1-5 stars) and reviews strictly linked to completed tokens ([`FDB-001`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l277)).
- `feedback_responses`: Official organization responses with moderation status.
- `support_cases`: Unified ticket domain for User-to-Org, User-to-Admin, and Org-to-Admin disputes ([`ARC-005`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l39), [`SUP-001`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l289)).
- `case_messages`: Threaded messages exchanged between parties.
- `case_attachments`: Authorized document and photo attachments.
- `knowledge_solutions`: Recommended response snippets and FAQ resolutions for operators.

### Domain 10: Devices & Public Displays
- `kiosk_devices`: Registered self-service hardware units with device activation tokens.
- `display_devices`: TV monitors and public display units (id, branch_id, device_token, name, status: `ONLINE`, `OFFLINE`).
- `display_configurations`: Layout settings (counters shown, split view, sound chime, announcement text).
- `device_commands`: Remote operational commands (`RELOAD`, `PAUSE`, `CLEAR_CACHE`).

### Domain 11: Platform Governance & Observability
- `notifications`: Multi-channel user notifications (email, SMS, in-app).
- `notification_templates`: Localized notification formats.
- `audit_events`: System-wide security and compliance event journal.
- `risk_flags`: Automated financial and account anomaly alerts.
- `feature_flags`: Controlled rollout switches for new modules.

---

## 4. Security & Access Control Architecture

### 4.1 Unified Principal Context
To replace overloaded role checks, authentication will resolve all requests into an explicit `AuthenticatedPrincipal`:

```python
class PrincipalType(str, Enum):
    SUPER_ADMIN = "super_admin"
    ORG_HEAD    = "org_head"
    MANAGER     = "manager"
    EMPLOYEE    = "employee"
    USER        = "user"
    DEVICE      = "device"

class AuthenticatedPrincipal(BaseModel):
    id: int
    principal_type: PrincipalType
    tenant_id: Optional[int] = None       # organization_id
    branch_id: Optional[int] = None       # branch_id
    permissions: set[str] = Field(default_factory=set)
    session_id: str
```

### 4.2 Declarative Permission & Tenant Dependencies
FastAPI dependencies will enforce boundary validation automatically:

```python
def require_permission(perm: str):
    async def dependency(principal: AuthenticatedPrincipal = Depends(get_current_principal)):
        if perm not in principal.permissions and principal.principal_type != PrincipalType.SUPER_ADMIN:
            raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
        return principal
    return dependency

def require_tenant_context():
    async def dependency(principal: AuthenticatedPrincipal = Depends(get_current_principal)):
        if not principal.tenant_id and principal.principal_type != PrincipalType.SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="Tenant context required")
        return principal.tenant_id
    return dependency
```

### 4.3 Cryptographically Signed QR Tokens ([`QR-002`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l259))
The insecure sequential URL (`/public/track/47`) will be replaced with a signed opaque token:
1. When a token is confirmed, a random 128-bit UUID is generated (`token_ref`).
2. An HMAC-SHA256 signature is calculated over `f"{token_ref}:{token_id}:{org_id}:{expiry_ts}"` using the server secret key.
3. The QR payload contains `f"/public/track/{token_ref}?sig={hmac_hex}"`.
4. The scanner validates the signature before returning queue status. Sequential ID scanning is completely neutralized.

### 4.4 CSRF Protection Architecture
FastAPI will be equipped with standard CSRF protection:
1. `CSRFMiddleware` injects an encrypted `csrf_token` cookie on GET requests.
2. Jinja2 templates embed `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">` in every form.
3. POST/PUT/DELETE requests validate the form token against the signed session cookie.

---

## 5. Real-Time Synchronization & WebSocket Bus

### 5.1 Restoring the Broadcast Pipeline
In [`app/services/token_service.py`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/services/token_service.py), all state transitions (`book_token`, `perform_token_action`) will explicitly trigger [`broadcast_queue_update`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/app/websocket/broadcaster.py#L15-L38):

```python
# Immediately following DB transaction commit:
await db.commit()
await broadcast_queue_update(db, org_id=token.organization_id, token_id=token.id)
```

### 5.2 Redis Pub/Sub Distributed Bus
To enable multi-worker production deployments (`uvicorn --workers 4`), the in-memory WebSocket manager will be abstracted to publish events to Redis channels:
- Channel `org:{org_id}:display` → received by all TV screens and operator dashboards.
- Channel `token:{token_id}:tracking` → received by individual user trackers.

---

## 6. Financial Ledger & Cash Reconciliation Pipeline

### 6.1 Double-Entry Ledger Invariant
For every monetary transaction, equal balancing entries are recorded in `ledger_entries`:
- **Online Booking (Razorpay):**
  - `DEBIT GATEWAY_ESCROW: ₹200.00`
  - `CREDIT ORG_REVENUE: ₹190.00`
  - `CREDIT PLATFORM_FEE: ₹10.00`
- **Cash Counter Booking:**
  - `DEBIT EMPLOYEE_CASH_DRAWER: ₹50.00`
  - `CREDIT ORG_REVENUE: ₹50.00`

### 6.2 Cash Shift Close Workflow
1. Employee initiates shift close: requests `counter_shifts` transition to `RECONCILING`.
2. Employee enters physical cash count.
3. System compares physical count to cumulative ledger debits for the shift.
4. If difference == 0, marked `MATCHED`. If difference != 0, flagged as `SHORTAGE` or `EXCESS` with mandatory explanation note.
5. Organization head reviews discrepancy and approves shift closure.

---

## 7. Public Kiosk Privacy Lifecycle ([`KSK-002`](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/FEATURE_REQUIREMENTS_MATRIX.md#l157))

To protect visitor privacy on shared public touchscreens:
1. When a token slip is displayed on the kiosk screen, a visible 20-second countdown timer initiates.
2. If no user interaction occurs during the countdown, the screen automatically flushes DOM memory and redirects to Step 1 (Select Service).
3. Guest name and mobile number fields are cleared from browser memory.
4. The kiosk page disables browser form auto-complete (`autocomplete="off"`).
