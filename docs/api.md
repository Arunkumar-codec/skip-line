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

## File Downloads

```http
GET /tokens/47/receipt      → PDF receipt (A6, with QR code)
GET /tokens/47/qr           → QR code PNG image
```

Both endpoints are accessible without authentication (public by design — allows kiosk guests to download).

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
