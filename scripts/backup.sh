#!/bin/sh
# scripts/backup.sh — logical MySQL backup (Phase 8, deployment hardening)
#
# Runs INSIDE a mysql:8.3 container (docker compose run --rm backup, or the
# `make backup` target) with mysqldump on PATH. Output lands in ./backups.
#
# Requires: DB_HOST, DB_NAME, DB_USER, DB_PASSWORD (compose supplies them).
# Questions about restore: see scripts/restore.sh and docs.

set -eu

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET_DIR="${BACKUP_DIR:-/backups}"
TARGET="${TARGET_DIR}/${DB_NAME:-skipline}_${TIMESTAMP}.sql.gz"

mkdir -p "${TARGET_DIR}"

echo "[backup] dumping ${DB_NAME:-skipline} from ${DB_HOST:-db}..."
mysqldump \
    --host="${DB_HOST:-db}" \
    --port="${DB_PORT:-3306}" \
    --user="${DB_USER:?DB_USER is required}" \
    --password="${DB_PASSWORD:?DB_PASSWORD is required}" \
    --single-transaction \
    --routines \
    --events \
    --triggers \
    --set-gtid-purged=OFF \
    "${DB_NAME:?DB_NAME is required}" | gzip > "${TARGET}"

# practical, honest integrity check: a gzip file that round-trips and is
# non-trivial in size (an empty dump is < ~1000 B even for an empty schema)
EXPECT_MIN_BYTES="${BACKUP_MIN_BYTES:-1000}"
BYTES="$(gzip -c -d "${TARGET}" | wc -c)"
if [ "${BYTES}" -lt "${EXPECT_MIN_BYTES}" ]; then
    echo "[backup] FAIL: ${TARGET} decompressed to ${BYTES} bytes " \
         "(< ${EXPECT_MIN_BYTES}) — refusing to call this a backup" >&2
    rm -f "${TARGET}"
    exit 1
fi

echo "[backup] OK: ${TARGET} ($(gzip -c -d "${TARGET}" | wc -c) bytes of SQL)"
echo "[backup] sha256:"
sha256sum "${TARGET}" || true

# retention: keep the newest N dumps (default 7), delete older ones
KEEP="${BACKUP_KEEP:-7}"
ls -1t "${TARGET_DIR}"/*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while IFS= read -r old; do
    echo "[backup] pruning old backup: ${old}"
    rm -f "${old}"
done
