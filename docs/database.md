# Skip Line — Database Documentation

Engine: MySQL 8.3 InnoDB | Charset: utf8mb4 | ORM: SQLAlchemy 2.0 async

---

## Tables Overview

| Table | Rows (typical) | Description |
|-------|---------------|-------------|
| `admins` | 1–5 | Platform administrators |
| `organizations` | Hundreds | Registered service orgs |
| `organization_verifications` | ~3 per org | Document verification records |
| `users` | Thousands | End users |
| `services` | ~5 per org | Services offered |
| `counters` | ~3 per org | Service counters |
| `tokens` | Millions | Queue tokens (core entity) |
| `token_history` | ~5 per token | Immutable status audit trail |
| `payments` | ~20% of tokens | Payment records |
| `notifications` | ~4 per token | User notifications |
| `queue_logs` | ~5 per token | Append-only action log |
| `settings` | ~10 per org | Per-org configuration |

---

## Key Relationships

```
admins ──────────────────────────── organizations (approves)
organizations ──┬──────────────── services
                ├──────────────── counters
                ├──────────────── tokens
                ├──────────────── payments
                ├──────────────── queue_logs
                └──────────────── settings

users ──────────┬──────────────── tokens
                ├──────────────── payments
                └──────────────── notifications

counters ───────┬──────────────── tokens
                └──────────────── queue_logs

services ───────┬──────────────── counters
                └──────────────── tokens

tokens ─────────┬──────────────── token_history
                ├──────────────── payments
                ├──────────────── notifications
                └──────────────── queue_logs
```

---

## Token Status State Machine

```
                    ┌─────────────────────────────┐
                    │                             │
        book()      ▼      call()                 │ recall()
  ──────────► WAITING ──────────► CALLED ─────────┘
                │                  │  │
          cancel│            hold  │  │ progress()
                │                  ▼  │
                │              ON_HOLD│
                │                  │  │ complete()
                │            call()│  ▼
                │                  COMPLETED
                │
                ├─────────────────► SKIPPED  (from WAITING or CALLED)
                └─────────────────► CANCELLED (from WAITING, CALLED, ON_HOLD)
```

---

## Important Indexes

```sql
-- Hot query: get waiting tokens for a counter (queue management)
INDEX idx_tokens_counter (counter_id)
INDEX idx_tokens_status  (status)

-- Hot query: user's active tokens
INDEX idx_tokens_user (user_id)

-- Hot query: org approval filter
INDEX idx_orgs_status (status)

-- Analytics: tokens by date
INDEX idx_tokens_booked (booked_at)

-- Audit: queue actions by org/date
INDEX idx_ql_org    (organization_id)
INDEX idx_ql_logged (logged_at)
```

---

## Settings Keys

The `settings` table stores per-org feature flags:

| Key | Values | Default | Description |
|-----|--------|---------|-------------|
| `payment_enabled` | `true`/`false` | `false` | Enable Razorpay |
| `sms_enabled` | `true`/`false` | `false` | Enable SMS |
| `kiosk_enabled` | `true`/`false` | `true` | Enable kiosk page |
| `token_recall_limit` | `1`–`10` | `3` | Max recalls per token |
| `announcement_text` | any string | — | Display screen banner |
| `queue_nearing_threshold` | `1`–`10` | `3` | Notify when N people ahead |

---

## Migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Create new migration (after model change)
alembic revision --autogenerate -m "description"

# Show current state
alembic current

# Show full history
alembic history --verbose
```

Migration files live in `migrations/versions/`.
The `migrations/env.py` reads connection string from `app.config.settings`
automatically — no manual editing needed.
