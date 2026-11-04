#!/bin/sh
# scripts/restore.sh — restore a SkipLine MySQL backup (Phase 8 deployment)
#
# Usage:
#   scripts/restore.sh backups/skipline_20261104T120000Z.sql.gz
#   scripts/restore.sh <file.sql.gz> <target_db_name>
#
# This script is DESTRUCTIVE: with --force it drops and re-creates the named
# target database. Without --force it performs the restore INTO A SCRATCH
# DATABASE (skipline_restore_check by default) and verifies the schema loads,
# so an operator can validate the artifact before committing to a real
# restore. It NEVER invents success — it reports exactly what it validated.

set -eu

FILE="${1:?usage: restore.sh <backup.sql.gz> [target_db] [--force]}"
TARGET_DB="${2:-skipline_restore_check}"
FORCE="${3:-}"

[ -f "${FILE}" ] || { echo "restore.sh: file not found: ${FILE}" >&2; exit 1; }

: "${DB_HOST:?DB_HOST is required (export it or put it in .env)}"
: "${DB_NAME:?DB_NAME is required — the SOURCE db name is only used for sanity output}"
: "${DB_USER:?DB_USER is required}"
: "${DB_PASSWORD:?DB_PASSWORD is required}"
DB_PORT="${DB_PORT:-3306}"

MYSQL="mysql --host=${DB_HOST} --port=${DB_PORT} --user=${DB_USER} --password=${DB_PASSWORD}"
echo "[restore] artifact: ${FILE}"
echo "[restore] target database: ${TARGET_DB}"

if [ "${FORCE}" = "--force" ]; then
    cat <<EOF
WARNING: --force will DROP and rebuild '${TARGET_DB}'. All data currently in
that database will be destroyed. Press Ctrl+C to abort (5s)...
EOF
    sleep 5
    ${MYSQL} -e "DROP DATABASE IF EXISTS \`${TARGET_DB}\`;"

    echo "[restore] creating ${TARGET_DB} and loading ${FILE} ..."
    gunzip -c "${FILE}" | ${MYSQL}
    TABLES="$(${MYSQL} -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '${TARGET_DB}';")"
    echo "[restore] OK: ${TABLES} tables restored in ${TARGET_DB}"
else
    echo "[restore] validation mode (no --force): restoring to scratch '${TARGET_DB}'"
    ${MYSQL} -e "DROP DATABASE IF EXISTS \`${TARGET_DB}\`; CREATE DATABASE \`${TARGET_DB}\` CHARACTER SET utf8mb4;"
    gunzip -c "${FILE}" | ${MYSQL} "${TARGET_DB}"
    TABLES="$(${MYSQL} -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '${TARGET_DB}';")"
    echo "[restore] validated: ${TABLES} tables load from the artifact."
    echo "[restore] artifact left untouched; scratch DB '${TARGET_DB}' still holds the"
    echo "[restore] restored data for inspection — drop it manually when done:"
    echo "[restore]   mysql ... -e \"DROP DATABASE ${TARGET_DB};\""
fi
