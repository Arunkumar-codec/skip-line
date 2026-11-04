# Skip Line — Makefile
# Usage: make <target>

.PHONY: help install dev migrate seed test lint docker-up docker-down clean \
        backup restore security-scan pip-audit

PYTHON_BIN ?= python

# Load the developer's .env (if present) so authenticated targets work without
# exporting variables manually. .env is gitignored and NEVER committed.
# Without .env, credential-requiring targets fail with a clear error instead
# of silently using an insecure built-in password.
ifneq ($(wildcard .env),)
include .env
export
endif

help:
	@echo "Skip Line — Available commands:"
	@echo "  make install     Install Python dependencies"
	@echo "  make dev         Start development server with auto-reload"
	@echo "  make migrate     Run Alembic database migrations"
	@echo "  make seed        Load development seed data"
	@echo "  make test        Run all tests"
	@echo "  make test-unit   Run unit tests only"
	@echo "  make test-api    Run API tests only"
	@echo "  make lint        Check code style"
	@echo "  make docker-up   Start full stack with Docker Compose"
	@echo "  make docker-down Stop Docker stack"
	@echo "  make backup      Dump the database to ./backups (requires stack up)"
	@echo "  make restore     Restore: make restore FILE=backups/xxx.sql.gz [MODE=check|force]"
	@echo "  make security-scan  Secret-pattern scan over tracked files"
	@echo "  make pip-audit      Dependency CVE audit (needs pip-audit installed)"
	@echo "  make clean       Remove generated files (qr_codes, receipts)"

install:
	pip install -r requirements.txt

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

migrate:
	alembic upgrade head

seed:
	mysql -u $${DB_USER:-skipline_user} -p"$${DB_PASSWORD:?DB_PASSWORD is required to seed - export it or add it to the .env file (make env creates one) }" $${DB_NAME:-skipline} < database/seed.sql

test:
	pytest

test-unit:
	pytest -m unit -v

test-api:
	pytest -m api -v

test-integration:
	pytest -m integration -v

test-cov:
	pytest --cov=app --cov=models --cov-report=html --cov-report=term-missing

lint:
	python -m py_compile app/**/*.py models/*.py
	@echo "Syntax check passed"

docker-up:
	docker compose up --build -d
	@echo "Stack started. App: http://localhost | Admin: http://localhost/admin/login"

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f app

docker-migrate:
	docker compose exec app alembic upgrade head

# ── Backups (Phase 8) ────────────────────────────────────────────────────────
# Logical dump through the compose `backup` one-shot service (mysqldump
# inside mysql:8.3). Output: ./backups/<db>_<timestamp>.sql.gz (gitignored).

backup:
	docker compose run --rm backup

# MODE=check (default): validate the artifact into a scratch database.
# MODE=force:  DESTRUCTIVE — drops and rebuilds the named database.
ifeq ($(MODE),force)
RESTORE_ARGS = --force
else
RESTORE_ARGS =
endif

restore:
	@if [ -z "$(FILE)" ]; then \
	  echo "usage: make restore FILE=backups/skipline_20261104T120000Z.sql.gz [MODE=force]" >&2; \
	  exit 2; \
	fi
	bash scripts/restore.sh "$(FILE)" $${RESTORE_DB:-skipline_restore_check} $(RESTORE_ARGS)

# ── Security tooling (Phase 8, SEC-008) ─────────────────────────────────────

security-scan:
	$(PYTHON_BIN) scripts/security_scan.py

pip-audit:
	$(PYTHON_BIN) scripts/security_scan.py --pip-audit

clean:
	find qr_codes -name "*.png" -delete 2>/dev/null || true
	find receipts  -name "*.pdf" -delete 2>/dev/null || true
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleaned up generated files"

env:
	cp .env.example .env
	@echo ".env created — edit it with your values before starting"
