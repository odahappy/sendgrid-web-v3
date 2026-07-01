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

TS="$(date +%Y%m%d_%H%M%S)"
OUT="$BACKUP_DIR/web_admin_scheduler_${TS}.db"
cp "$DB_PATH" "$OUT"

tar -czf "$BACKUP_DIR/uploads_${TS}.tar.gz" uploads 2>/dev/null || true

echo "Database backup: $OUT"
echo "Uploads backup: $BACKUP_DIR/uploads_${TS}.tar.gz"
