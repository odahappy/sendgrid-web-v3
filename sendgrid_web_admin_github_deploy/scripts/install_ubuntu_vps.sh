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
  echo "sudo is required on Ubuntu VPS."
  exit 1
fi

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

  pgrep -x apt >/dev/null 2>&1 \
    || pgrep -x apt-get >/dev/null 2>&1 \
    || pgrep -x dpkg >/dev/null 2>&1 \
    || pgrep -f '/usr/bin/unattended-upgrade' >/dev/null 2>&1 \
    || pgrep -f 'unattended-upgr' >/dev/null 2>&1
}

wait_for_apt() {
  local started_at now elapsed
  started_at="$(date +%s)"

  while apt_is_busy; do
    now="$(date +%s)"
    elapsed=$((now - started_at))

    if [ "$elapsed" -ge "$APT_LOCK_TIMEOUT" ]; then
      echo "ERROR: apt/dpkg remained busy for ${APT_LOCK_TIMEOUT} seconds."
      ps -ef | grep -E '[a]pt|[d]pkg|[u]nattended' || true
      return 1
    fi

    echo "apt/dpkg is busy, waiting... (${elapsed}s/${APT_LOCK_TIMEOUT}s)"
    sleep 5
  done
}

repair_dpkg() {
  wait_for_apt
  DEBIAN_FRONTEND=noninteractive "${SUDO[@]}" dpkg --configure -a || true
}

apt_retry() {
  local attempt=1
  local rc=0

  while [ "$attempt" -le "$APT_RETRIES" ]; do
    wait_for_apt
    echo "Running apt-get $* (attempt ${attempt}/${APT_RETRIES})..."

    if DEBIAN_FRONTEND=noninteractive "${SUDO[@]}" apt-get \
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

echo "Installing OS packages..."
apt_retry update
apt_retry install -y python3 python3-venv python3-pip rsync curl ca-certificates psmisc

echo "Copying project to ${APP_DIR} ..."
"${SUDO[@]}" mkdir -p "$APP_DIR"
"${SUDO[@]}" rsync -a --delete \
  --exclude '.venv' \
  --exclude 'data' \
  --exclude 'uploads' \
  --exclude 'logs' \
  --exclude '.git' \
  "$SRC_DIR"/ "$APP_DIR"/

"${SUDO[@]}" mkdir -p "$APP_DIR/data" "$APP_DIR/uploads/templates" "$APP_DIR/logs"
"${SUDO[@]}" chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"

cd "$APP_DIR"

if [ ! -f .env ]; then
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

  sed -i "s|ADMIN_PASSWORD=CHANGE_THIS_ADMIN_PASSWORD|ADMIN_PASSWORD=${ADMIN_PASSWORD}|" .env
  sed -i "s|SECRET_KEY=CHANGE_THIS_LONG_RANDOM_SECRET_KEY|SECRET_KEY=${SECRET_KEY}|" .env
  sed -i "s|DATA_ENCRYPTION_KEY=CHANGE_THIS_LONG_RANDOM_DATA_KEY|DATA_ENCRYPTION_KEY=${DATA_ENCRYPTION_KEY}|" .env
  sed -i "s|SERVICE_TOKEN=CHANGE_THIS_LONG_RANDOM_SERVICE_TOKEN|SERVICE_TOKEN=${SERVICE_TOKEN}|" .env

  echo "Generated .env with random production secrets."
  echo "Initial admin login: admin / ${ADMIN_PASSWORD}"
else
  echo ".env already exists, keeping existing configuration."
fi

ADMIN_PASSWORD_PRINT="$(grep '^ADMIN_PASSWORD=' "$APP_DIR/.env" | cut -d= -f2- || true)"

echo "Creating Python virtual environment..."
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
echo "Creating systemd service: ${SERVICE_FILE}"

sed \
  -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
  -e "s|__SERVICE_GROUP__|${SERVICE_GROUP}|g" \
  -e "s|__APP_DIR__|${APP_DIR}|g" \
  -e "s|__HOST__|${HOST}|g" \
  -e "s|__PORT__|${PORT}|g" \
  deploy/systemd/sendgrid-web-admin.service.template | "${SUDO[@]}" tee "$SERVICE_FILE" >/dev/null

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable "$SERVICE_NAME"
"${SUDO[@]}" systemctl restart "$SERVICE_NAME"

if command -v ufw >/dev/null 2>&1 && [ "${EXPOSE_APP_PORT}" = "true" ]; then
  "${SUDO[@]}" ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
fi

sleep 2

"${SUDO[@]}" systemctl --no-pager --full status "$SERVICE_NAME" || true

PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"

if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
  HEALTH_STATUS="OK"
else
  HEALTH_STATUS="CHECK_SERVICE_LOG"
fi

echo ""
echo "============================================================"
echo "Installed successfully."
if [ "${HOST}" = "127.0.0.1" ] || [ "${HOST}" = "localhost" ]; then
  echo "Access URL: local reverse proxy required (recommended: run scripts/setup_https.sh)"
else
  echo "Access URL: http://${PUBLIC_IP}:${PORT}"
fi
echo "ADMIN_PASSWORD=${ADMIN_PASSWORD_PRINT}"
echo ""
echo "Health check: ${HEALTH_STATUS}"
echo "Service: ${SERVICE_NAME}"
echo "Check logs: sudo journalctl -u ${SERVICE_NAME} -f"
echo "Config file: ${APP_DIR}/.env"
echo "============================================================"
