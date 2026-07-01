#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/sendgrid-web-admin}"
SERVICE_NAME="${SERVICE_NAME:-sendgrid-web-admin}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$USER}}"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
HOST="${SERVER_HOST:-0.0.0.0}"
PORT="${SERVER_PORT:-8080}"

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required on Ubuntu VPS."
  exit 1
fi

cd "$(dirname "$0")/.."
SRC_DIR="$(pwd)"

echo "Installing OS packages..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip rsync curl ca-certificates

echo "Copying project to ${APP_DIR} ..."
sudo mkdir -p "$APP_DIR"
sudo rsync -a --delete \
  --exclude '.venv' \
  --exclude 'data' \
  --exclude 'uploads' \
  --exclude 'logs' \
  --exclude '.git' \
  "$SRC_DIR"/ "$APP_DIR"/

sudo mkdir -p "$APP_DIR/data" "$APP_DIR/uploads/templates" "$APP_DIR/logs"
sudo chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"

cd "$APP_DIR"

if [ ! -f .env ]; then
  cp .env.vps.example .env
  ADMIN_PASSWORD="$(python3 - <<'PY'
import secrets, string
alphabet = string.ascii_letters + string.digits
print(''.join(secrets.choice(alphabet) for _ in range(16)))
PY
)"
  SECRET_KEY="$(python3 - <<'PY'
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
  sed -i "s|SERVICE_TOKEN=CHANGE_THIS_LONG_RANDOM_SERVICE_TOKEN|SERVICE_TOKEN=${SERVICE_TOKEN}|" .env
  echo "Generated .env with random production secrets."
  echo "Initial admin login: admin / ${ADMIN_PASSWORD}"
else
  echo ".env already exists, keeping existing configuration."
fi

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
  deploy/systemd/sendgrid-web-admin.service.template | sudo tee "$SERVICE_FILE" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
fi

sleep 2
sudo systemctl --no-pager --full status "$SERVICE_NAME" || true

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
echo "Access URL: http://${PUBLIC_IP}:${PORT}"
echo "Health check: ${HEALTH_STATUS}"
echo "Service: ${SERVICE_NAME}"
echo "Check logs: sudo journalctl -u ${SERVICE_NAME} -f"
echo "Config file: ${APP_DIR}/.env"
echo "============================================================"
