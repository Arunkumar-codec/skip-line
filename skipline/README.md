# Skip Line — Virtual Queue Management System

A production-ready FastAPI multi-tenant SaaS platform for managing physical queues digitally.
Hospitals, banks, government offices, temples and more can manage queues while users book tokens online.

---

## Quick Start

### 1. Prerequisites
- Python 3.11+
- MySQL 8.0+
- pip

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure environment
```bash
cp .env.example .env
# Edit .env with your database credentials and secrets
```

### 4. Create the database
```bash
mysql -u root -p -e "CREATE DATABASE skipline CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -u root -p -e "CREATE USER 'skipline_user'@'localhost' IDENTIFIED BY 'password';"
mysql -u root -p -e "GRANT ALL ON skipline.* TO 'skipline_user'@'localhost';"
```

### 5. Run migrations
```bash
alembic upgrade head
```
Or use the bootstrap script (creates tables + seeds admin):
```bash
python scripts/bootstrap.py
```

### 6. Start the server
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## Default Admin Credentials
- **URL**: http://localhost:8000/admin/login
- **Email**: admin@skipline.app
- **Password**: Admin@1234!

*(Change these in `.env` before production deployment)*

---

## Portal URLs
| Portal | URL |
|--------|-----|
| Admin | http://localhost:8000/admin/login |
| Organization | http://localhost:8000/org/login |
| User | http://localhost:8000/user/login |
| Public Display | http://localhost:8000/public/display/{org_id} |
| Kiosk | http://localhost:8000/user/kiosk/{org_id} |
| API Docs | http://localhost:8000/api/docs |

---

## Features

### Admin Panel
- Advanced dashboard with charts (tokens, revenue)
- Organization approval / rejection / suspension
- User management
- Revenue reports (30-day chart)
- Queue stats reports
- System settings
- Audit logs

### Organization Portal
- Dashboard with live queue stats
- Queue management (call, skip, complete, hold tokens)
- Counter management (online / physical / hybrid)
- Service management with pricing
- Staff management (Owner / Counter Employee / Kiosk roles)
- Monthly scheduling & holiday management
- Reports (daily volume, status breakdown, top services, counter performance)
- Payment history and revenue tracking
- Settings (announcements, hours, etc.)

### Token System
- **Online tokens** — booked by registered users via web
- **Physical / Walk-in tokens** — issued at counter for guests
- **Kiosk tokens** — self-service touch-screen booking
- **Future bookings** — advance scheduling
- Capacity allocation (online vs physical per counter)
- QR code per token (view on screen, full-screen, download separately — no auto-download)
- PDF receipt / slip (download separately)
- Real-time tracking via WebSocket

### User Portal
- Registration & login
- Extended profile (DOB, gender, ID documents, emergency contact)
- Active token dashboard with live QR viewer
- Token history
- Organization search (by name, type, city)
- In-app notifications

### Public Pages
- Live token tracking page (WebSocket)
- Organization display screen (for TV/monitor)
- Kiosk booking page (touch-screen friendly)

### Payments
- Razorpay integration (online)
- Cash collection tracking
- Settlement reports
- Refund support

### QR UX (Fixed)
- Tokens display QR on screen in a modal
- Full-screen QR view button
- Download QR button (explicit, no auto-download)
- Download Slip (PDF) as a separate action

---

## Project Structure
```
skipline/
├── app/
│   ├── main.py              # FastAPI app factory
│   ├── config.py            # Pydantic settings
│   ├── database.py          # Async SQLAlchemy engine
│   ├── dependencies.py      # Shared FastAPI dependencies
│   ├── middleware/          # Logging, rate-limiting
│   ├── routers/             # API + HTML view routers
│   ├── schemas/             # Pydantic request/response models
│   ├── services/            # Business logic layer
│   ├── utils/               # JWT, security, QR, PDF
│   └── websocket/           # WS manager, broadcaster, heartbeat
├── models/                  # SQLAlchemy ORM models
├── templates/               # Jinja2 HTML templates
│   ├── shared/              # base.html, sidebars, login
│   ├── admin/               # Admin portal pages
│   ├── org/                 # Organization portal pages
│   ├── user/                # User portal pages
│   └── public/              # Public display, kiosk, track pages
├── static/                  # CSS, JS, images
├── migrations/              # Alembic migration files
├── scripts/                 # bootstrap.py, seed_data.py
├── qr_codes/                # Generated QR PNGs
├── receipts/                # Generated PDF receipts
├── uploads/                 # User-uploaded files
├── requirements.txt
├── alembic.ini
└── .env.example
```

---

## Environment Variables
See `.env.example` for full reference. Key variables:

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | App secret (min 32 chars) |
| `JWT_SECRET_KEY` | JWT signing secret |
| `DB_HOST/DB_NAME/DB_USER/DB_PASSWORD` | MySQL connection |
| `RAZORPAY_KEY_ID/SECRET` | Payment gateway |
| `ADMIN_EMAIL/PASSWORD` | Bootstrap admin credentials |

---

## Known Fixes Applied
1. **Broken logout links** — sidebars now point to role-specific logout routes (`/admin/logout`, `/org/logout`, `/user/logout`) instead of the POST-only `/auth/logout`
2. **QR auto-download** — QR codes now display in a modal; user explicitly clicks "Download QR" or "Download Slip" as separate actions
3. **Jinja2 undefined variables** — all template context variables verified and passed (`total_revenue`, `pages`, `daily_labels`, `status_breakdown`, `counter_stats`, `top_services`, `city`, `state`, `success`, `error`, etc.)
4. **JSON serialization errors** — ORM objects serialised to plain dicts before passing to `tojson` filter in templates
5. **Missing routes** — added `/admin/tokens`, `/admin/reports/revenue`, `/admin/reports/queue`, `/admin/settings`, `/user/tokens/active`
6. **Lazy-load async errors** — `selectinload` added for all relationships accessed in templates
7. **Dashboard/API mismatch** — login now redirects to HTML view routes, not API routes
