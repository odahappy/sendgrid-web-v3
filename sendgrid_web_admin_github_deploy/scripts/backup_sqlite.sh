#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-backups}"
mkdir -p "$BACKUP_DIR"

DB_PATH="data/web_admin_scheduler.db"
if [ -f .env ]; then
  DB_PATH_ENV="$(grep -E '^DATABASE_PATH=' .env | tail -1 | cut -d= -f2- || true)"
  if [ -n "$DB_PATH_ENV" ]; then
    DB_PATH="$DB_PATH_ENV"
  fi
fi

if [ ! -f "$DB_PATH" ]; then
  echo "Database not found: $DB_PATH"
  exit 1
fi

TS="$(date +%Y%m%d_%H%M%S)_$$"
OUT="$BACKUP_DIR/web_admin_scheduler_${TS}.db"
UPLOADS_OUT="$BACKUP_DIR/uploads_${TS}.tar.gz"

# SQLite's backup API produces a transactionally consistent copy even in WAL mode.
python3 - "$DB_PATH" "$OUT" <<'PY'
import sqlite3
import sys

source_path, destination_path = sys.argv[1], sys.argv[2]
source = sqlite3.connect(source_path, timeout=30)
destination = sqlite3.connect(destination_path)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
PY

echo "Database backup: $OUT"
if [ -d uploads ]; then
  if ! tar -czf "$UPLOADS_OUT" uploads; then
    rm -f "$UPLOADS_OUT"
    echo "ERROR: Uploads backup failed. Database backup remains at $OUT" >&2
    exit 1
  fi
  echo "Uploads backup: $UPLOADS_OUT"
else
  echo "Uploads backup skipped: uploads/ does not exist."
fi
