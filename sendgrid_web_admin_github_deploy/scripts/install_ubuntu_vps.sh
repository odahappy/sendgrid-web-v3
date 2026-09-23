#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/sendgrid-web-admin}"
SERVICE_NAME="${SERVICE_NAME:-sendgrid-web-admin}"

SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$USER}}"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

HOST="${SERVER_HOST:-127.0.0.1}"
EXPOSE_APP_PORT="${EXPOSE_APP_PORT:-false}"
PORT="${SERVER_PORT:-8080}"

APT_LOCK_TIMEOUT="${APT_LOCK_TIMEOUT:-900}"
APT_RETRIES="${APT_RETRIES:-5}"
APT_RETRY_DELAY="${APT_RETRY_DELAY:-10}"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=()
elif command -v sudo >/dev/null 2>&1; then
  SUDO=(sudo)
else
  echo "ERROR: sudo is required on Ubuntu VPS."
  exit 1
fi

# ------------------------------------------------------------
# Detect REAL apt/dpkg activity.
#
# Ubuntu 24.04 can permanently run:
#
# unattended-upgrade-shutdown --wait-for-signal
#
# That process does NOT mean apt is currently locked.
# ------------------------------------------------------------
apt_is_busy() {
  local lock_file

  if command -v fuser >/dev/null 2>&1; then
    for lock_file in \
      /var/lib/dpkg/lock-frontend \
      /var/lib/dpkg/lock \
      /var/cache/apt/archives/lock \
      /var/lib/apt/lists/lock; do

      if [ -e "$lock_file" ] && fuser "$lock_file" >/dev/null 2>&1; then
        return 0
      fi
    done
  fi

  if pgrep -x apt >/dev/null 2>&1 \
    || pgrep -x apt-get >/dev/null 2>&1 \
    || pgrep -x dpkg >/dev/null 2>&1; then
    return 0
  fi

  return 1
}

show_apt_debug() {
  local lock_file

  echo ""
  echo "Active apt/dpkg processes:"
  ps -ef | grep -E '[a]pt|[d]pkg' || true

  echo ""
  echo "Package lock holders:"

  if command -v fuser >/dev/null 2>&1; then
    for lock_file in \
      /var/lib/dpkg/lock-frontend \
      /var/lib/dpkg/lock \
      /var/cache/apt/archives/lock \
      /var/lib/apt/lists/lock; do

      if [ -e "$lock_file" ]; then
        echo "--- ${lock_file} ---"
        fuser -v "$lock_file" 2>/dev/null || true
      fi
    done
  else
    echo "fuser is not installed."
  fi
}

wait_for_apt() {
  local started_at now elapsed

  started_at="$(date +%s)"

  while apt_is_busy; do

    now="$(date +%s)"
    elapsed=$((now - started_at))

    if [ "$elapsed" -ge "$APT_LOCK_TIMEOUT" ]; then
      echo ""
      echo "ERROR: apt/dpkg remained busy for ${APT_LOCK_TIMEOUT} seconds."
      show_apt_debug
      return 1
    fi

    echo "apt/dpkg is busy, waiting... (${elapsed}s/${APT_LOCK_TIMEOUT}s)"

    sleep 5
  done
}

repair_dpkg() {
  echo "Checking dpkg state..."

  wait_for_apt

  DEBIAN_FRONTEND=noninteractive \
    "${SUDO[@]}" \
    dpkg --configure -a || true
}

apt_retry() {
  local attempt=1
  local rc=0

  while [ "$attempt" -le "$APT_RETRIES" ]; do

    wait_for_apt

    echo "Running apt-get $* (attempt ${attempt}/${APT_RETRIES})..."

    if DEBIAN_FRONTEND=noninteractive \
      "${SUDO[@]}" \
      apt-get \
      -o DPkg::Lock::Timeout=120 \
      -o Acquire::Retries=3 \
      "$@"; then

      return 0
    else
      rc=$?
    fi

    echo "WARNING: apt-get failed with exit code ${rc}."

    repair_dpkg

    if [ "$attempt" -lt "$APT_RETRIES" ]; then
      echo "Retrying in ${APT_RETRY_DELAY} seconds..."
      sleep "$APT_RETRY_DELAY"
    fi

    attempt=$((attempt + 1))
  done

  echo "ERROR: apt-get $* failed after ${APT_RETRIES} attempts."

  return "$rc"
}

cd "$(dirname "$0")/.."

SRC_DIR="$(pwd)"

echo "============================================================"
echo "Installing SendGrid Web Admin application"
echo "Source: ${SRC_DIR}"
echo "Target: ${APP_DIR}"
echo "Service: ${SERVICE_NAME}"
echo "Port: ${PORT}"
echo "Host: ${HOST}"
echo "============================================================"

echo ""
echo "Installing OS packages..."

apt_retry update

apt_retry install -y \
  python3 \
  python3-venv \
  python3-pip \
  rsync \
  curl \
  ca-certificates \
  psmisc

echo ""
echo "Copying project to ${APP_DIR} ..."

"${SUDO[@]}" mkdir -p "$APP_DIR"

# Keep the live configuration and a consistent SQLite snapshot before changing
# application files.  The backup must succeed before rsync is allowed to run.
DB_EXCLUDES=()
if "${SUDO[@]}" test -f "$APP_DIR/.env" \
  || "${SUDO[@]}" test -f "$APP_DIR/data/web_admin_scheduler.db"; then
  "${SUDO[@]}" mkdir -p -m 700 "$APP_DIR/backups"
  PRE_INSTALL_BACKUP="$("${SUDO[@]}" mktemp -d "$APP_DIR/backups/pre-install-$(date +%Y%m%d_%H%M%S)-XXXXXXXX")"

  if "${SUDO[@]}" test -f "$APP_DIR/.env"; then
    "${SUDO[@]}" install -m 600 "$APP_DIR/.env" "$PRE_INSTALL_BACKUP/.env"
  fi

  # The application resolves relative DATABASE_PATH values from APP_DIR. Use
  # sqlite3.backup so that writes in WAL mode are included in the snapshot.
  DB_RELATIVE_PATH="$("${SUDO[@]}" python3 - "$APP_DIR" "$PRE_INSTALL_BACKUP" <<'PY'
import os
import pathlib
import shlex
import sqlite3
import sys

app_dir = pathlib.Path(sys.argv[1])
backup_dir = pathlib.Path(sys.argv[2])
configured_path = 'data/web_admin_scheduler.db'
env_file = app_dir / '.env'
if env_file.is_file():
    for line in env_file.read_text(encoding='utf-8').splitlines():
        setting = line.strip()
        if setting.startswith('export '):
            setting = setting[7:].lstrip()
        if setting.startswith('DATABASE_PATH='):
            parts = shlex.split(setting.partition('=')[2], comments=True)
            if len(parts) != 1 or not parts[0] or '$' in parts[0]:
                raise SystemExit('Cannot safely back up DATABASE_PATH in .env')
            configured_path = parts[0]

db_path = pathlib.Path(configured_path)
if not db_path.is_absolute():
    db_path = app_dir / db_path
db_path = pathlib.Path(os.path.abspath(db_path))

if db_path.is_file():
    source = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=30)
    destination = sqlite3.connect(backup_dir / 'database.db')
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    (backup_dir / 'database-path.txt').write_text(str(db_path) + '\n', encoding='utf-8')
elif db_path.exists():
    raise SystemExit(f'Configured database path is not a file: {db_path}')

try:
    print(db_path.relative_to(pathlib.Path(os.path.abspath(app_dir))))
except ValueError:
    pass
PY
)"

  if [ -n "$DB_RELATIVE_PATH" ]; then
    DB_EXCLUDES+=(
      --exclude "/$DB_RELATIVE_PATH"
      --exclude "/$DB_RELATIVE_PATH-wal"
      --exclude "/$DB_RELATIVE_PATH-shm"
    )
  fi
  echo "Pre-install snapshot: ${PRE_INSTALL_BACKUP}"
fi

"${SUDO[@]}" rsync \
  -a \
  --delete \
  --exclude '.venv' \
  --exclude 'data' \
  --exclude 'uploads' \
  --exclude 'logs' \
  --exclude '/.env' \
  --exclude '/backups' \
  --exclude '.git' \
  "${DB_EXCLUDES[@]}" \
  "$SRC_DIR"/ \
  "$APP_DIR"/

"${SUDO[@]}" mkdir -p \
  "$APP_DIR/data" \
  "$APP_DIR/uploads/templates" \
  "$APP_DIR/logs"

"${SUDO[@]}" chown \
  -R \
  "$SERVICE_USER:$SERVICE_GROUP" \
  "$APP_DIR"

cd "$APP_DIR"

if [ ! -f .env ]; then

  echo "Creating production .env..."

  cp .env.vps.example .env

  ADMIN_PASSWORD="$(python3 - <<'PY'
import secrets
import string

alphabet = string.ascii_letters + string.digits
print(''.join(secrets.choice(alphabet) for _ in range(16)))
PY
)"

  SECRET_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"

  DATA_ENCRYPTION_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"

  SERVICE_TOKEN="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(40))
PY
)"

  sed -i \
    "s|ADMIN_PASSWORD=CHANGE_THIS_ADMIN_PASSWORD|ADMIN_PASSWORD=${ADMIN_PASSWORD}|" \
    .env

  sed -i \
    "s|SECRET_KEY=CHANGE_THIS_LONG_RANDOM_SECRET_KEY|SECRET_KEY=${SECRET_KEY}|" \
    .env

  sed -i \
    "s|DATA_ENCRYPTION_KEY=CHANGE_THIS_LONG_RANDOM_DATA_KEY|DATA_ENCRYPTION_KEY=${DATA_ENCRYPTION_KEY}|" \
    .env

  sed -i \
    "s|SERVICE_TOKEN=CHANGE_THIS_LONG_RANDOM_SERVICE_TOKEN|SERVICE_TOKEN=${SERVICE_TOKEN}|" \
    .env

  echo "Generated .env with random production secrets."
  echo "Initial admin login:"
  echo "Username: admin"
  echo "Password: ${ADMIN_PASSWORD}"

else

  echo ".env already exists."
  echo "Keeping existing configuration."

fi

"${SUDO[@]}" chmod 600 .env

ADMIN_PASSWORD_PRINT="$(
  grep '^ADMIN_PASSWORD=' "$APP_DIR/.env" \
    | cut -d= -f2- \
    || true
)"

echo ""
echo "Creating Python virtual environment..."

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
else
  echo "Existing virtual environment found."
fi

echo ""
echo "Upgrading pip..."

.venv/bin/python \
  -m pip \
  install \
  --upgrade \
  pip

echo ""
echo "Installing Python requirements..."

.venv/bin/python \
  -m pip \
  install \
  -r requirements.txt

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo ""
echo "Creating systemd service:"
echo "${SERVICE_FILE}"

sed \
  -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
  -e "s|__SERVICE_GROUP__|${SERVICE_GROUP}|g" \
  -e "s|__APP_DIR__|${APP_DIR}|g" \
  -e "s|__HOST__|${HOST}|g" \
  -e "s|__PORT__|${PORT}|g" \
  deploy/systemd/sendgrid-web-admin.service.template \
  | "${SUDO[@]}" tee "$SERVICE_FILE" >/dev/null

echo ""
echo "Reloading systemd..."

"${SUDO[@]}" systemctl daemon-reload

echo "Enabling service..."

"${SUDO[@]}" systemctl enable "$SERVICE_NAME"

echo "Restarting service..."

"${SUDO[@]}" systemctl restart "$SERVICE_NAME"

if command -v ufw >/dev/null 2>&1 \
  && [ "${EXPOSE_APP_PORT}" = "true" ]; then

  "${SUDO[@]}" ufw allow "${PORT}/tcp" \
    >/dev/null 2>&1 || true

fi

sleep 2

echo ""
echo "Service status:"
"${SUDO[@]}" systemctl \
  --no-pager \
  --full \
  status "$SERVICE_NAME" || true

PUBLIC_IP="$(
  curl \
    -fsS \
    --max-time 5 \
    https://api.ipify.org \
    2>/dev/null \
    || hostname -I | awk '{print $1}'
)"

HEALTH_URL="http://127.0.0.1:${PORT}/api/health"

echo ""
echo "Checking application health..."

if curl \
  -fsS \
  --max-time 5 \
  "$HEALTH_URL" \
  >/dev/null 2>&1; then

  HEALTH_STATUS="OK"

else

  HEALTH_STATUS="CHECK_SERVICE_LOG"

fi

echo ""
echo "============================================================"
echo "Installed successfully."
echo ""

if [ "${HOST}" = "127.0.0.1" ] \
  || [ "${HOST}" = "localhost" ]; then

  echo "Access URL:"
  echo "Local reverse proxy required."
  echo "Recommended: run scripts/setup_https.sh"

else

  echo "Access URL:"
  echo "http://${PUBLIC_IP}:${PORT}"

fi

echo ""
echo "ADMIN_USERNAME=admin"
echo "ADMIN_PASSWORD=${ADMIN_PASSWORD_PRINT}"
echo ""
echo "Health check: ${HEALTH_STATUS}"
echo ""
echo "Service: ${SERVICE_NAME}"
echo ""
echo "Check logs:"
echo "sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "Config file:"
echo "${APP_DIR}/.env"
echo ""
echo "============================================================"
