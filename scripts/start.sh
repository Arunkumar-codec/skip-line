#!/usr/bin/env bash
# Skip Line — Production startup script
# Runs Alembic migrations then starts Uvicorn.
# Used as the Docker CMD and as a systemd ExecStart wrapper.

set -euo pipefail

echo "=============================="
echo " Skip Line — Starting up"
echo "=============================="

# ── Environment check ──────────────────────────────────────────────────────
: "${DB_HOST:?DB_HOST must be set}"
: "${DB_NAME:?DB_NAME must be set}"
: "${JWT_SECRET_KEY:?JWT_SECRET_KEY must be set}"

# ── Wait for MySQL to be ready ─────────────────────────────────────────────
echo "[1/3] Waiting for MySQL at ${DB_HOST}:${DB_PORT:-3306}…"
max_retries=30
retries=0
until python -c "
import pymysql, sys
try:
    pymysql.connect(host='${DB_HOST}', port=int('${DB_PORT:-3306}'),
                    user='${DB_USER}', password='${DB_PASSWORD}', db='${DB_NAME}')
    sys.exit(0)
except Exception as e:
    print(f'  Not ready: {e}')
    sys.exit(1)
" 2>/dev/null; do
  retries=$((retries+1))
  if [ "$retries" -ge "$max_retries" ]; then
    echo "ERROR: MySQL did not become ready in time. Aborting."
    exit 1
  fi
  sleep 2
done
echo "  MySQL is ready."

# ── Run migrations ─────────────────────────────────────────────────────────
echo "[2/3] Running Alembic migrations…"
alembic upgrade head
echo "  Migrations complete."

# ── Start application ──────────────────────────────────────────────────────
WORKERS="${UVICORN_WORKERS:-4}"
PORT="${APP_PORT:-8000}"

echo "[3/3] Starting Uvicorn — workers=${WORKERS} port=${PORT}"
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers "$WORKERS" \
  --log-level "${LOG_LEVEL:-info}" \
  --access-log \
  --proxy-headers \
  --forwarded-allow-ips "*"
