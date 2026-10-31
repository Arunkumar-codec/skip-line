# Skip Line — Makefile
# Usage: make <target>

.PHONY: help install dev migrate seed test lint docker-up docker-down clean

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

clean:
	find qr_codes -name "*.png" -delete 2>/dev/null || true
	find receipts  -name "*.pdf" -delete 2>/dev/null || true
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleaned up generated files"

env:
	cp .env.example .env
	@echo ".env created — edit it with your values before starting"
