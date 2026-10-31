# Skip Line — API Documentation

Base URL: `http://localhost:8000`
Interactive docs (development only): `http://localhost:8000/api/docs`

---

## Authentication

All protected endpoints accept authentication via:

1. **Cookie** (browser) — set automatically on login
2. **Authorization header** (API clients): `Authorization: Bearer <token>`

### Login — Admin
```http
POST /auth/admin/login
Content-Type: application/json

{"email": "admin@skipline.app", "password": "Admin@1234!"}
```
```json
{"access_token": "eyJ...", "token_type": "bearer", "role": "admin", "entity_id": 1, "name": "Super Admin"}
```

### Login — Organization
```http
POST /auth/org/login
Content-Type: application/json

{"email": "hospital@example.com", "password": "OrgPass@123"}
```

### Login — User
```http
POST /auth/user/login
Content-Type: application/json

{"email": "user@example.com", "password": "UserPass@123"}
```

---

## Organization Registration

```http
POST /org/register
Content-Type: application/json

{
  "name": "City General Hospital",
  "registration_number": "MH-HOSP-001",
  "org_type": "hospital",
  "address": "1 Hospital Road",
  "city": "Mumbai",
  "state": "Maharashtra",
  "email": "hospital@example.com",
  "mobile": "8012345678",
  "password": "OrgPass@123",
  "confirm_password": "OrgPass@123"
}
```
Response: `status: "pending"` — awaits admin approval.

Valid `org_type` values: `hospital`, `clinic`, `bank`, `government`, `temple`, `educational`, `service_center`, `other`

---

## Counters

### Create Counter
```http
POST /org/counters
Authorization: Bearer <org_token>

{
  "counter_name": "OPD Counter 1",
  "counter_number": 1,
  "token_prefix": "A",
  "daily_limit": 200,
  "service_id": 3
}
```

### Update Counter Status
```http
PUT /org/counters/1
Authorization: Bearer <org_token>

{"status": "break"}
```
Valid statuses: `active`, `inactive`, `break`

---

## Token Lifecycle

### Book Token (authenticated user)
```http
POST /user/tokens/book
Authorization: Bearer <user_token>

{"organization_id": 1, "counter_id": 2, "service_id": 3}
```
```json
{
  "data": {
    "id": 47,
    "token_number": "A-023",
    "status": "waiting",
    "queue_position": 5,
    "qr_code_path": "qr_codes/token_47_org_1.png"
  }
}
```

### Book Token (kiosk guest)
```http
POST /public/kiosk/book

{
  "organization_id": 1,
  "counter_id": 2,
  "guest_name": "Arjun Sharma",
  "guest_mobile": "9876543210"
}
```

Slot-mode bookings (with `slot_id`) additionally enforce the same-day cutoff (SCH-005):
a booking whose slot start instant has already passed returns `400` with the detail
`"This time slot has already started and can no longer be booked."`. The public
slot-date calendar also stops advertising capacity of started slots.

The issuing channel is recorded server-side (SCH-004, CTR-003): kiosk bookings are
`kiosk`, `/user/tokens/book` bookings are `online`, and no request schema accepts a
booking-source field from the client. Organization capacity rules
(`daily_token`, `walk_in_quota`, `per_user`, `priority_reserve` at org/branch/counter/
service scope) are enforced as layered checks inside the booking transaction and
reject with `400` (`e.g.` `"Daily booking limit reached for this counter."`); a
rejected booking consumes no slot occupancy.

### Slot Ladder (per-date availability)
```http
GET /public/organizations/1/slots?date=2026-11-03&counter_id=2
```

Complements the per-date bookability calendar (`GET /public/organizations/{id}/slot-dates`)
with the per-slot selection surface (SCH-005/SCH-009). Optional `date` (business date
in the organization timezone, `YYYY-MM-DD`, default = organization's today; must lie
within the booking horizon), optional `counter_id` (unknown/foreign counters → `404`)
and `branch_id`. The response renders every slot of the date in the organization's/
branch's IANA timezone (`start_local`/`end_local` wall times + `timezone_name`, plus
the absolute `start_utc`/`end_utc` instants), with `capacity`, `remaining`
(`capacity − booked − held`) and `is_bookable`. Slots whose start instant has passed
are never listed (same-day cutoff); filled slots show `remaining: 0` and
`is_bookable: false`. Non-operational days return the day `state`
(`closed`/`holiday`/`not_open`/`service_unavailable`) with an empty slot list;
legacy day-queue tenants (no published schedule) return `is_legacy_day_queue: true`
and no slots — book those without `slot_id`.
```json
{
  "data": {
    "date": "2026-11-03",
    "state": "available",
    "opens_at": "10:00:00",
    "closes_at": "14:00:00",
    "timezone_name": "Asia/Kolkata",
    "is_legacy_day_queue": false,
    "slots": [
      {
        "id": 12,
        "counter_id": 2,
        "counter_number": 2,
        "counter_name": "OPD Counter 1",
        "slot_date": "2026-11-03",
        "start_local": "10:00:00",
        "end_local": "11:00:00",
        "start_utc": "2026-11-03T04:30:00",
        "end_utc": "2026-11-03T05:30:00",
        "capacity": 10,
        "remaining": 10,
        "is_bookable": true
      }
    ]
  }
}
```

### Token Actions (organization operator)
```http
POST /org/tokens/47/action
Authorization: Bearer <org_token>

{"action": "call"}
```

Valid actions and their preconditions:

| Action | Valid from status | Resulting status |
|--------|-------------------|-----------------|
| `call` | `waiting`, `on_hold` | `called` |
| `recall` | `called` | `called` |
| `hold` | `called`, `in_progress` | `on_hold` |
| `progress` | `called` | `in_progress` |
| `complete` | `called`, `in_progress` | `completed` |
| `skip` | `waiting`, `called` | `skipped` |
| `cancel` | `waiting`, `called`, `on_hold` | `cancelled` |

---

## Live Tracking

### Get tracking data
```http
GET /user/tokens/47/track
```
```json
{
  "data": {
    "token_id": 47,
    "token_number": "A-023",
    "status": "waiting",
    "queue_position": 3,
    "people_ahead": 2,
    "estimated_wait_minutes": 20,
    "current_token_at_counter": "A-021",
    "counter_name": "OPD Counter 1"
  }
}
```

### WebSocket (real-time)
```
ws://host/ws/token/47
```
Receives `{"type": "tracking_update", "data": {...}}` on every queue change.

---

## Display Screen

### HTTP (initial load)
```http
GET /public/display/1
```

### WebSocket (live)
```
ws://host/ws/org/1
```
Receives:
```json
{
  "type": "display_update",
  "data": {
    "counters": [
      {
        "counter_id": 1,
        "counter_name": "OPD Counter 1",
        "counter_number": 1,
        "status": "active",
        "current_token": "A-023",
        "upcoming_tokens": ["A-024","A-025","A-026"]
      }
    ],
    "announcement": "Welcome to City General Hospital"
  }
}
```

---

## Payments (Razorpay)

### 1. Initiate payment
```http
POST /payments/initiate
Authorization: Bearer <user_token>

{"token_id": 47}
```
```json
{
  "data": {
    "order_id": "order_ABC123",
    "amount": 20000,
    "currency": "INR",
    "key_id": "rzp_test_xxx"
  }
}
```
Use `order_id` and `key_id` with the Razorpay Checkout JS SDK.

### 2. Verify payment
```http
POST /payments/verify
Authorization: Bearer <user_token>

{
  "razorpay_order_id": "order_ABC123",
  "razorpay_payment_id": "pay_XYZ789",
  "razorpay_signature": "hmac_sha256_signature"
}
```

---

## Finance & Ledgering (Phase 5)

All money movement is recorded in an immutable double-entry ledger
(`ledger_entries`; entries are inserted, never updated — corrections are new
adjustment entries). Every finance endpoint is tenant-scoped: an organization
principal (`get_current_org`) always crosses `organization_id` with the caller,
and admin endpoints require `get_current_admin` (Bearer admin token). Amounts
are decimal strings.

### Ledger journal & totals (LED-001..004) — organization

```http
GET /org/api/finance/ledger?account=gateway&entry_type=charge&page=1&per_page=25
GET /org/api/finance/totals
```
- `ledger` returns a paginated `Pagination` body whose rows are
  `LedgerEntryRead`: `id, organization_id, account, entry_type, direction`
  (`debit`|`credit`), `amount, currency, token_id, payment_id,
  counter_shift_id, actor_type, actor_id, source_transaction_ref,
  description, entry_metadata, created_at`.
- `totals` returns `LedgerTotalsRead` derived **only** from ledger entries:
  `gross_revenue, cash_collected, gateway_collected, refunds, tax_collected,
  platform_fee, payouts_disbursed, net_revenue`.

### Financial breakdown report (CASH-003) — organization

```http
GET /org/api/finance/reports?service_id=5&counter_id=2
```
Returns gross revenue, refunds, discounts, tax, platform fees, net
organization revenue, pending/unreconciled cash and payout figures split by
date/service/counter/booking source. The platform-fee figure is derived
from ledger entries; there is **no** fee-configuration feature (Open
Decision — not required by the authoritative requirements).

### Cash sessions (CASH-001..002) — organization

```http
GET  /org/api/finance/cash-sessions?status=open&page=1
POST /org/api/finance/cash-sessions/{session_id}/close

{
  "counted_cash": 4850.00,          // >= 0, required
  "denominations": {"500": 9, "100": 35},   // optional
  "notes": "Closing remarks"        // optional, <= 500 chars
}
```
Close evaluates `discrepancy = counted_cash - expected` and files the session
as `matched`, `shortage`, `excess` or `pending_verification`; head/supervisor
reconciliation of the underlying shift is the Phase 4 SHIFT-005 flow.

### Payment refunds (PAY-003, FIN-001) — organization

```http
GET  /org/api/finance/refunds?page=1
POST /org/api/finance/refunds/{refund_id}/approve
POST /org/api/finance/refunds/{refund_id}/reject?rejection_reason=Kyc%20failed

POST /payments/{payment_id}/refund        (organization principal)

{
  "amount": 150.00,                // > 0, required
  "reason": "Service not rendered",// 3..500 chars, required
  "idempotency_key": "rcpt-88"     // optional, <= 100 chars
}
```
`POST /payments/{payment_id}/refund` requests an **auto-approved** idempotent
refund: resubmitting the same `idempotency_key` returns the existing refund
(no duplicate charge movement). `approve`/`reject` drive the
`pending_approval → processed|rejected` workflow (`400` on illegal
transitions; rejection requires a reason ≥ 3 chars). Successful refunds post
reversal ledger entries and mark the payment `partially_refunded`/`refunded`.
Rows are `RefundRead`: `id, organization_id, payment_id, token_id, amount,
currency, reason, status, gateway_refund_id, idempotency_key,
requested_by_*, approved_by_*, rejection_reason, processed_at, created_at`.

### Payment records & history — organization

```http
GET /payments/org/history?page=1&per_page=20
```
Paginated `PaymentRead` rows: `id, token_id, amount, currency,
payment_method, status, razorpay_order_id, razorpay_payment_id, paid_at,
created_at`.

### Razorpay webhook (PAY-003, PAY-005) — **unauthenticated, signed**

```http
POST /payments/webhook
X-Razorpay-Signature: <hmac_sha256_of_raw_body>
X-Razorpay-Event-Id: <event id>
Content-Type: application/json

{ "event": "payment.captured", "payload": { ... } }
```
- Signature: HMAC-SHA256 of the **raw request body** using
  `razorpay_webhook_secret` (falls back to `razorpay_key_secret`); if neither
  is configured the check passes so test environments work — production must
  set a secret.
- Replay safety: `webhook_events.event_id` (UNIQUE) deduplicates; a repeated
  event returns `{"status": "already_processed"}` without re-processing.
- Single confirmation (PAY-005): `payment.captured`/`order.paid` confirm a
  payment only when it is not already paid (no duplicate revenue), promote the
  held slot reservation, and post the gateway→revenue double-entry ledger
  charge.
- FIN-003: after the idempotent confirmation, organization-scoped risk rules
  run (unusual amount, volume, duplicate references, after-hours, payment-
  token consistency) — flagging failures never break the confirmation.
- Errors: `400` invalid signature, `400` malformed JSON.

### Bank account (FIN-002) — organization

```http
GET  /org/api/finance/bank-account
POST /org/api/finance/bank-account

{
  "account_number": "123456789012",      // 8..30 chars
  "ifsc_code": "HDFC0001234",            // 5..20 chars
  "account_holder_name": "Finance Head", // 2..150 chars
  "bank_name": "HDFC Bank"               // 2..100 chars
}
```
Any account change applies a **72-hour payout security hold** (`held_until`),
notifies the owner in-app and writes an audit event. Response/status is
`OrgBankAccountRead`: `id, organization_id, account_number, ifsc_code,
account_holder_name, bank_name, status, held_until, hold_reason, verified_at,
created_at`.

### Payouts (FIN-001) — organization view

```http
GET /org/api/finance/payouts?page=1&per_page=20
```
Paginated `PayoutRead`: `id, organization_id, amount, currency, status
(scheduled|held|processing|paid|failed), hold_reason, period_start,
period_end, gateway_payout_id, paid_at, created_at`.

### Payment method policy & paid-booking pause (FIN-001)

Policy is a nullable JSON list on the organization (`allowed_payment_methods`);
values must come from the `PaymentMethod` vocabulary
(`razorpay|upi|cash|waived|free|pay_at_counter`). **NULL = unrestricted**
(backward compatible); an empty list is rejected (pass `null` to lift
restrictions). Enforcement runs in `payment_service.create_payment_order`
(online checkout) and `shift_service.book_walkin_token` (counter walk-ins:
cash / pay-at-counter / online → validated as `razorpay` / waived); free
services are not subject to the policy. Rejections: `403` on
`/payments/initiate`, `400`/`403` on walk-in booking.

```http
GET  /org/api/finance/payment-policy                (organization, read-only)

GET  /admin/api/organizations/{org_id}/payment-policy            (admin)
PUT  /admin/api/organizations/{org_id}/payment-policy            (admin)

{
  "allowed_payment_methods": ["razorpay", "cash"]   // null = unrestricted
}
```
`PUT` replaces the whole policy (audited as `payment.policy.updated`);
vocabulary violations or an empty list → `422`.

Paid-booking pause: a finance admin can pause a **paid** booking
(`amount_paid > 0`, status `waiting|called|in_progress`). The token moves to
the existing `on_hold` state (capacity stays consumed — pause is not a
cancellation) with the operator's previous status remembered; while paused,
`call`/`progress`/`complete` are rejected from every endpoint
(`403 PermissionError` from the token-action engine). Both actions require
a reason (≥ 3 chars) and write `token.payment_hold.paused` /
`token.payment_hold.resumed` audit events.

```http
POST /admin/tokens/{token_id}/pause        (admin)
POST /admin/tokens/{token_id}/resume       (admin)

{ "reason": "Payment review pending" }     // 3..500 chars, required
```
Responses are `PaidBookingHoldRead`: `token_id, token_number,
organization_id, status, payment_hold, payment_hold_reason,
payment_hold_at, payment_hold_by, payment_hold_previous_status`.
Errors: `404` unknown token; `400` only-paid / status / duplicate-pause /
not-paused / short-reason; `403` policy or operator-action rejections.

### Admin financial management (FIN-001..004, LED-004) — admin

All require `Authorization: Bearer <admin_token>`.

```http
GET  /admin/api/finance/payouts?org_id=3&status=held&page=1
POST /admin/payouts/{payout_id}/hold                      {"reason": "KYC review pending"}   // 3..500 chars, required
POST /admin/payouts/{payout_id}/release                   (held → scheduled)
POST /admin/payouts/{payout_id}/disburse                  (scheduled → paid; posts the immutable payout double entry)
GET  /admin/api/finance/risk-flags?org_id=3&status=flagged&page=1
POST /admin/risk-flags/{flag_id}/resolve                  {"status": "resolved|dismissed", "resolution_notes": "..."}  // notes 3..500 chars, required
GET  /admin/api/finance/bank-accounts?status=pending_verification&page=1
POST /admin/bank-accounts/{org_id}/verify                  (verified → lifts 72h hold)
GET  /admin/api/finance/overview
```
- `hold`/`release`/`resolve`/`verify` all write audit events (`payout.held`,
  `payout.released`, `risk.resolved`, bank verification) with the acting
  admin's identity; `400` on illegal state transitions.
- `/admin/api/finance/overview` returns payout-status counts, open risk-flag
  count, pending bank verifications, and **ledger-derived** per-organization
  totals (`gross_revenue, cash_collected, gateway_collected, refunds,
  platform_fee, net_revenue`) plus their aggregate.
- The admin page behind these endpoints is `GET /admin/finance`
  (server-rendered, cookie session) — payouts review (hold/release/disburse),
  risk-flag review, bank-verification queue and ledger overview
  (`templates/admin/finance.html`).

---

## File Downloads

```http
GET /tokens/47/receipt      → PDF receipt (A6, with QR code)
GET /tokens/47/qr           → QR code PNG image
```

Both endpoints are accessible without authentication (public by design — allows kiosk guests to download).

---

## Schedule-Change Impact Workflow (SCH-007)

Organization schedule edits (weekly schedules, date exceptions, capacity
rules) that affect confirmed bookings are never applied silently. The edit
is **proposed**, its impact computed, and explicit per-booking decisions
(honor / reschedule / cancel) are recorded and executed on confirmation.
All endpoints require an organization principal (`get_current_org`).

### 1. Create a change preview (proposal only — nothing is applied)

```http
POST /org/schedules/changes/preview
{
  "entity": "weekly_schedule",      // weekly_schedule | schedule_exception | capacity_rule
  "edit_type": "update",            // create | update | delete
  "target_id": 5,                   // required for update / delete
  "data": {                         // same payload the CRUD endpoint accepts
    "open_time": "10:00:00",
    "close_time": "16:00:00"
  }
}
```

Response carries the computed **impact report**: proposed vs. existing slot
grids per day, affected confirmed bookings (`impact.affected_bookings`),
occupancy conflicts (proposed capacity below existing occupancy), and a
64-char `impact_digest` fingerprint of the affected state. A preview is
valid for **120 minutes**.

### 2. Record per-booking decisions

```http
POST /org/schedules/changes/{preview_id}/resolve
{ "decisions": { "token-uuid-or-id": { "decision": "cancel", "reason": "..." } } }
```

Decision values: `honor` (no change), `reschedule` (recorded but **blocked
at execution** until OPEN_DECISIONS Decision 16 resolves slot-capacity
release), `cancel` (slot place stays consumed per Decision 16/13A).

### 3. Confirm or reject

```http
POST /org/schedules/changes/{preview_id}/confirm
POST /org/schedules/changes/{preview_id}/reject
```

Confirmation re-verifies the impact digest server-side and applies the
configuration, executes decisions, notifies affected users (in-app,
`schedule_change` type) and writes the audit entry **in one transaction**.
Results:
- `200` — applied exactly once (`status: confirmed`, `applied_summary`)
- `404` — unknown/cross-tenant preview
- `409` — already resolved / **stale** (state changed since the preview) /
  **expired** (TTL) / **blocked** (missing decisions)
- `400` — invalid proposal

`GET /org/api/schedules/changes?status=pending` lists previews;
`GET /org/schedules/changes/{id}` shows one.

---

## Daily Counter Shifts (SHIFT-001/002)

Daily counter operational shifts: the operator submits an open request and the
organization head (or an employee explicitly delegated via the RBAC
`shifts:approve` permission — the organization principal always retains the
authority) approves/rejects **with reason**. A shift is never active before an
approval, and the requesting employee activates their own approved shift to
start the desk.

Lifecycle: `open_requested` → `approved` → `active` (later tasks add the
closing workflow: `paused`, `close_requested`, `reconciling`, `closed`). A
rejection keeps the row `open_requested` with the recorded reason so the
operator can correct and resubmit.

### 1. Submit / correct the open request (employee)

```http
POST /employee/shifts/open-request
Authorization: Bearer <employee_token>
{
  "counter_id": 3,
  "services": [7, 9],
  "device_ready": true,
  "opening_cash": "500.00",
  "remarks": "Morning shift"
}
```

- `shift_date` is resolved server-side to the tenant's current business day
  (organization timezone, Decision 12) — never taken from client input.
- The counter must belong to the employee's organization and be active;
  requested services must exist in the tenant. Unknown/cross-tenant targets
  are `404`.
- Resubmitting while pending updates the same row (correction flow).

### 2. Decide (head / delegated manager)

```http
POST /org/shifts/{shift_id}/decision
Authorization: Bearer <org_token | employee_token>
{
  "decision": "approve",              // approve | reject
  "reason": "Device verified, float confirmed",
  "approved_services": [7],
  "approved_period_start": "09:00",   // optional, tenant-local
  "approved_period_end": "17:30",
  "payment_permitted": true
}
```

- Approvals default the approved services to the requested list;
  `decided_by_type` records `organization` or `employee` (delegated).
- Rejections keep `status: open_requested` with `decided_reason`.
- `200` on decision, `404` unknown/cross-tenant, `409` row not pending,
  `400` missing reason / invalid scope.
- Listing: `GET /org/shifts?status=open_requested&shift_date=2026-10-29`
  (also aliased at `/org/api/shifts`).

### 3. Activate the approved desk (employee)

```http
POST /employee/shifts/{shift_id}/activate
Authorization: Bearer <employee_token>
```

Only the requesting employee, only an `approved` shift, only for the
tenant's current day (and inside the approved period when one was set).
`409` for pending/already-active, `403` for another operator's shift.

`GET /employee/shifts?status=active` lists the operator's own rows.

### Gate primitive

`shift_service.require_active_shift(db, org_id, counter_id, employee_id)`
raises when the operator has no active approved shift — counter operations
that need a desk (token issuance, payment collection, queue changes) call
this guard (SHIFT-003 wiring is a separate Phase 4 task).

---

## Pagination

All list endpoints support:

```
?page=1&per_page=20
```

Response:
```json
{
  "total": 150,
  "page": 2,
  "per_page": 20,
  "pages": 8,
  "data": [...]
}
```

---

## Error Responses

```json
{"detail": "Token not found"}
{"detail": "Cannot complete a token with status 'waiting'. Expected one of: ['called', 'in_progress']"}
{"detail": "Counter daily limit reached"}
{"detail": "Invalid payment signature"}
```

HTTP status codes used:
- `200` — success
- `400` — business logic error
- `401` — not authenticated
- `403` — wrong role or inactive account
- `404` — resource not found
- `422` — validation error (Pydantic)
- `429` — rate limit exceeded
