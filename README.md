# Skip Line — Digital Queue Management & Virtual Token System

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-green.svg)](https://fastapi.tiangolo.com)
[![MySQL 8](https://img.shields.io/badge/MySQL-8.3-orange.svg)](https://mysql.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> A production-ready digital queue management platform. Users book virtual tokens remotely, track their position in real time via WebSocket, and receive notifications when their turn approaches — eliminating physical waiting lines.

---

## Table of Contents

1. [Features](#features)
2. [Tech Stack](#tech-stack)
3. [Architecture](#architecture)
4. [Quick Start](#quick-start)
5. [Installation Guide](#installation-guide)
6. [Environment Variables](#environment-variables)
7. [Database Setup](#database-setup)
8. [Running the Application](#running-the-application)
9. [Docker Deployment](#docker-deployment)
10. [API Reference](#api-reference)
11. [User Roles](#user-roles)
12. [WebSocket Protocol](#websocket-protocol)
13. [Project Structure](#project-structure)
14. [Testing](#testing)
15. [Production Deployment](#production-deployment)

---

## Features

| Feature | Description |
|---------|-------------|
| **Virtual Tokens** | Auto-generated tokens (A-001, B-042) with QR codes |
| **Real-Time Queue** | WebSocket live updates — no page refresh needed |
| **Public Display Screen** | Fullscreen TV display showing current + upcoming tokens |
| **Self-Service Kiosk** | Walk-in guests book tokens without an account |
| **PDF Receipts** | Professional A6 receipts with QR code, downloadable instantly |
| **Razorpay Payments** | Optional per-service payment with UPI/card support |
| **Role-Based Access** | Admin · Organization · User — fully isolated portals |
| **Email Notifications** | Token created, called, completed via SMTP |
| **Admin Dashboard** | Org approval, analytics, revenue reports |
| **Org Dashboard** | Counter management, queue operations, live stats |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI 0.111, SQLAlchemy 2.0 |
| Database | MySQL 8.3, Alembic migrations |
| Frontend | Jinja2 templates, Bootstrap 5.3, Chart.js 4 |
| Auth | JWT (python-jose), bcrypt (passlib) |
| Real-Time | WebSocket (FastAPI native) |
| QR Codes | qrcode + Pillow |
| PDF | ReportLab |
| Payments | Razorpay SDK |
| Email | aiosmtplib |
| Containerisation | Docker, Docker Compose, Nginx |
| Testing | pytest-asyncio, httpx |

---

## Architecture

```
Browser / Mobile / Kiosk / Display Screen
              │
              ▼
         Nginx (reverse proxy, rate limiting, WebSocket upgrade)
              │
              ▼
    FastAPI Application (Python 3.12)
    ├── Jinja2 HTML routers   (browser pages)
    ├── REST API routers      (JSON API)
    ├── WebSocket endpoints   (real-time)
    └── JWT middleware        (authentication)
              │
    ┌─────────┼─────────┐
    ▼         ▼         ▼
Services   Models    Utils
(business  (ORM)     (QR, PDF,
 logic)               JWT, bcrypt)
              │
              ▼
    MySQL 8.3 via SQLAlchemy async (aiomysql)
```

---

## Quick Start

```bash
# 1. Clone and enter the project
git clone <repo-url> skip-line
cd skip-line

# 2. Copy environment file and edit it
cp .env.example .env
# Edit .env: set DB_PASSWORD, JWT_SECRET_KEY, etc.

# 3. Start with Docker Compose (recommended)
docker compose up --build

# The app is now live at http://localhost
# Admin panel: http://localhost/admin/login
# Default credentials: admin@skipline.app / Admin@1234!
```

---

## Installation Guide

### Prerequisites

- Python 3.12+
- MySQL 8.3+ (or Docker)
- Git

### Manual Installation

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate       # Linux / macOS
.venv\Scripts\activate          # Windows

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your database credentials and secrets

# Create the database
mysql -u root -p -e "CREATE DATABASE skipline CHARACTER SET utf8mb4;"

# Run database migrations
alembic upgrade head

# (Optional) Load seed data for development
mysql -u skipline_user -p skipline < database/seed.sql

# Start the development server
uvicorn app.main:app --reload --port 8000
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_KEY` | ✅ | Session signing key (64 random chars) |
| `JWT_SECRET_KEY` | ✅ | JWT signing key (64 random chars) |
| `DB_HOST` | ✅ | MySQL host |
| `DB_NAME` | ✅ | Database name |
| `DB_USER` | ✅ | Database user |
| `DB_PASSWORD` | ✅ | Database password |
| `RAZORPAY_KEY_ID` | ⬜ | Razorpay key (only if payments enabled) |
| `RAZORPAY_KEY_SECRET` | ⬜ | Razorpay secret |
| `MAIL_USERNAME` | ⬜ | SMTP username (email notifications) |
| `MAIL_PASSWORD` | ⬜ | SMTP app password |
| `ADMIN_EMAIL` | ✅ | Default admin email (seeded on first run) |
| `ADMIN_PASSWORD` | ✅ | Default admin password — **change immediately** |

Generate secure keys:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Database Setup

```bash
# Apply all migrations
alembic upgrade head

# Check migration status
alembic current

# Create a new migration after model changes
alembic revision --autogenerate -m "add_column_xyz"

# Rollback one step
alembic downgrade -1
```

The full SQL DDL is also available at `database/schema.sql` for direct import.

---

## Running the Application

```bash
# Development (auto-reload)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Production (4 workers)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

# With Gunicorn (recommended for production)
gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

---

## Docker Deployment

```bash
# Build and start all services (app + MySQL + Nginx)
docker compose up --build -d

# View logs
docker compose logs -f app

# Run migrations inside the container
docker compose exec app alembic upgrade head

# Stop all services
docker compose down

# Stop and remove volumes (WARNING: deletes all data)
docker compose down -v
```

### Services

| Service | Port | Description |
|---------|------|-------------|
| `nginx` | 80, 443 | Reverse proxy |
| `app` | 8000 (internal) | FastAPI application |
| `db` | 3306 (internal) | MySQL 8.3 |

---

## API Reference

All API endpoints return JSON in the format:
```json
{
  "success": true,
  "message": "OK",
  "data": { ... }
}
```

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/admin/login` | Admin login → JWT + cookie |
| POST | `/auth/org/login` | Org login → JWT + cookie |
| POST | `/auth/user/login` | User login → JWT + cookie |
| POST | `/auth/logout` | Clear all auth cookies |

### Admin

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/admin/dashboard` | Admin | Stats and analytics |
| GET | `/admin/organizations` | Admin | List all orgs |
| POST | `/admin/organizations/{id}/approve` | Admin | Approve org |
| POST | `/admin/organizations/{id}/reject` | Admin | Reject org |
| POST | `/admin/organizations/{id}/suspend` | Admin | Suspend org |
| DELETE | `/admin/organizations/{id}` | Admin | Delete org |

### Organization

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/org/register` | Public | Register new org |
| GET | `/org/dashboard` | Org | Dashboard stats |
| GET/POST | `/org/counters` | Org | List / create counters |
| PUT/DELETE | `/org/counters/{id}` | Org | Update / delete counter |
| GET/POST | `/org/services` | Org | List / create services |
| POST | `/org/tokens/{id}/action` | Org | Call/complete/skip token |

### User

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/user/register` | Public | Register user |
| GET | `/user/organizations/search` | Public | Search organizations |
| POST | `/user/tokens/book` | User | Book a token |
| GET | `/user/tokens/history` | User | Token history |
| GET | `/user/tokens/{id}/track` | Public | Live tracking data |

### Public (no auth)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/public/kiosk/book` | Guest token booking |
| GET | `/public/display/{org_id}` | Display screen data |
| GET | `/public/track/{token_id}` | Token tracking data |

### Tokens & Payments

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/tokens/{id}/receipt` | Optional | Download PDF receipt |
| GET | `/tokens/{id}/qr` | Optional | Download QR image |
| POST | `/payments/initiate` | User | Create Razorpay order |
| POST | `/payments/verify` | User | Verify payment signature |

---

## User Roles

### Admin
- Login at `/admin/login`
- Approve/reject/suspend organizations
- View system-wide analytics and revenue
- Manage all users and tokens

### Organization
- Register at `/org/register` (requires admin approval)
- Login at `/org/login`
- Create counters and services
- Operate the queue (call/complete/skip tokens)
- View the public display screen at `/org/display`
- Embed a kiosk at `/user/kiosk/{org_id}`

### User
- Register at `/user/register`
- Search organizations and book tokens
- Track queue position in real time
- Download PDF receipts
- Receive email notifications

### Guest (no account)
- Book tokens at kiosk page: `/user/kiosk/{org_id}`
- Track token via QR scan: `/public/track/{token_id}`

---

## WebSocket Protocol

### Org Display Room
```
Connect:  ws://host/ws/org/{org_id}
Receives: {"type": "display_update", "data": {...counters...}}
          {"type": "announcement",   "data": {"announcement": "text"}}
          {"type": "ping"}
Send:     "ping"  →  server replies "pong"
```

### Token Tracking Room
```
Connect:  ws://host/ws/token/{token_id}
Receives: {"type": "tracking_update", "data": {
              "token_number": "A-023",
              "status": "called",
              "queue_position": 1,
              "people_ahead": 0,
              "estimated_wait_minutes": 0,
              "current_token_at_counter": "A-022",
              "counter_name": "OPD Counter 1"
           }}
          {"type": "ping"}
```

---

## Project Structure

```
skip-line/
├── app/
│   ├── main.py              # Application factory + lifespan
│   ├── config.py            # Settings from .env
│   ├── database.py          # Async SQLAlchemy engine
│   ├── dependencies.py      # get_current_admin/org/user
│   ├── middleware/
│   │   ├── logging.py       # Request/response logging
│   │   └── rate_limit.py    # Sliding window rate limiter
│   ├── routers/
│   │   ├── auth.py          # Login/logout (all roles)
│   │   ├── admin.py         # Admin REST API
│   │   ├── org.py           # Organization REST API
│   │   ├── user.py          # User REST API
│   │   ├── public.py        # Public endpoints (kiosk, display)
│   │   ├── tokens.py        # Token file downloads
│   │   ├── payments.py      # Razorpay payment flow
│   │   ├── notifications.py # User notifications
│   │   ├── websocket.py     # WS endpoint routes
│   │   ├── views_admin.py   # Admin HTML pages
│   │   ├── views_org.py     # Org HTML pages
│   │   └── views_user.py    # User HTML pages + public pages
│   ├── services/
│   │   ├── admin_service.py
│   │   ├── org_service.py
│   │   ├── user_service.py
│   │   ├── token_service.py  # Core queue logic + state machine
│   │   ├── notification_service.py
│   │   └── payment_service.py
│   ├── utils/
│   │   ├── jwt.py           # Token creation/decoding
│   │   ├── security.py      # bcrypt hashing
│   │   ├── qr_generator.py  # QR code PNG generation
│   │   └── pdf_generator.py # ReportLab receipt generation
│   ├── websocket/
│   │   ├── manager.py       # Connection rooms + broadcast
│   │   ├── broadcaster.py   # High-level broadcast helpers
│   │   └── heartbeat.py     # Background keep-alive task
│   └── schemas/
│       ├── common.py        # APIResponse[T], PaginatedResponse[T]
│       ├── auth.py
│       ├── organization.py
│       ├── user.py
│       ├── service.py
│       ├── counter.py
│       ├── token.py
│       └── payment.py
├── models/
│   ├── admin.py
│   ├── organization.py
│   ├── organization_verification.py
│   ├── user.py
│   ├── service.py
│   ├── counter.py
│   ├── token.py             # Central entity — full status enum
│   ├── token_history.py     # Immutable audit trail
│   ├── payment.py
│   ├── notification.py
│   ├── queue_log.py         # Append-only action log
│   └── setting.py           # Per-org key-value config
├── templates/
│   ├── shared/              # base.html, sidebars, login
│   ├── admin/               # dashboard, organizations, users
│   ├── org/                 # dashboard, queue, counters, services
│   ├── user/                # dashboard, organizations, history
│   └── public/              # index, display, kiosk, track
├── static/
│   ├── css/main.css         # Full design system CSS
│   └── js/
│       ├── main.js          # Global utilities + WS factory
│       ├── queue-operator.js
│       ├── display-screen.js
│       └── token-tracker.js
├── database/
│   ├── schema.sql           # Full MySQL DDL (12 tables)
│   ├── init.sql             # Docker DB init
│   └── seed.sql             # Development seed data
├── migrations/              # Alembic migration versions
├── tests/
│   ├── conftest.py          # Async fixtures, test DB
│   ├── unit/                # Pure logic tests
│   ├── api/                 # HTTP endpoint tests
│   └── integration/         # Full lifecycle tests
├── nginx/nginx.conf         # Production Nginx config
├── Dockerfile               # Multi-stage production build
├── docker-compose.yml       # Full stack compose
├── requirements.txt
├── alembic.ini
├── pytest.ini
└── .env.example
```

---

## Testing

```bash
# Run all tests
pytest

# Run only unit tests (no I/O, very fast)
pytest -m unit

# Run API tests
pytest -m api

# Run integration tests
pytest -m integration

# With coverage report
pytest --cov=app --cov=models --cov-report=html

# Open coverage report
open htmlcov/index.html
```

---

## Production Deployment

### Security checklist

- [ ] Set `APP_ENV=production` and `APP_DEBUG=false`
- [ ] Generate strong `SECRET_KEY` and `JWT_SECRET_KEY` (64+ chars)
- [ ] Change default `ADMIN_PASSWORD` immediately after first login
- [ ] Configure SSL/TLS in Nginx (uncomment HTTPS redirect)
- [ ] Set `ALLOWED_HOSTS` to your actual domain
- [ ] Enable firewall: only ports 80/443 public, 3306 internal only
- [ ] Set up automated MySQL backups (daily dump to S3/GCS)
- [ ] Configure log rotation for `/app/logs`
- [ ] Set `RAZORPAY_KEY_ID` to live keys (not test)

### Scaling

For high-traffic deployments:
- Replace in-memory WebSocket manager with **Redis pub/sub**
- Replace in-memory rate limiter with **Redis** backend
- Use **Gunicorn** with multiple Uvicorn workers
- Add a **CDN** for static assets
- Consider **read replicas** for analytics queries

---

## License

MIT License — see [LICENSE](LICENSE) for details.
